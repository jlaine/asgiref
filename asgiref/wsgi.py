from io import BytesIO

from asgiref.sync import AsyncToSync, sync_to_async


class WsgiToAsgi:
    """
    Wraps a WSGI application to make it into an ASGI application.
    """

    def __init__(self, wsgi_application):
        self.wsgi_application = wsgi_application

    async def __call__(self, scope, receive, send):
        """
        ASGI application instantiation point.
        We return a new WsgiToAsgiInstance here with the WSGI app
        and the scope, ready to respond when it is __call__ed.
        """
        await WsgiToAsgiInstance(self.wsgi_application)(scope, receive, send)


class WsgiToAsgiInstance:
    """
    Per-socket instance of a wrapped WSGI application
    """

    def __init__(self, wsgi_application):
        self.wsgi_application = wsgi_application
        self.response_started = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            raise ValueError("WSGI wrapper received a non-HTTP scope")
        self.scope = scope
        # Alright, wait for the http.request message
        message = await receive()
        if message["type"] != "http.request":
            raise ValueError("WSGI wrapper received a non-HTTP-request message")
        # Wrap send so it can be called from the subthread
        self.sync_send = AsyncToSync(send)
        # Call the WSGI app
        await self.run_wsgi_app(message)

    def build_environ(self, scope, message):
        """
        Builds a scope and request message into a WSGI environ object.
        """
        environ = {
            "REQUEST_METHOD": scope["method"],
            "SCRIPT_NAME": "",
            "PATH_INFO": scope["path"],
            "QUERY_STRING": scope["query_string"].decode("ascii"),
            "SERVER_PROTOCOL": "HTTP/%s" % scope["http_version"],
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": scope.get("scheme", "http"),
            "wsgi.input": BytesIO(message.get("body", b"")),
            "wsgi.errors": BytesIO(),
            "wsgi.multithread": True,
            "wsgi.multiprocess": True,
            "wsgi.run_once": False,
        }
        # Get server name and port - required in WSGI, not in ASGI
        if "server" in scope:
            environ["SERVER_NAME"] = scope["server"][0]
            environ["SERVER_PORT"] = str(scope["server"][1])
        else:
            environ["SERVER_NAME"] = "localhost"
            environ["SERVER_PORT"] = "80"

        if "client" in scope:
            environ["REMOTE_ADDR"] = scope["client"][0]

        # Go through headers and make them into environ entries
        for name, value in self.scope.get("headers", []):
            name = name.decode("latin1")
            if name == "content-length":
                corrected_name = "CONTENT_LENGTH"
            elif name == "content-type":
                corrected_name = "CONTENT_TYPE"
            else:
                corrected_name = "HTTP_%s" % name.upper().replace("-", "_")
            # HTTPbis say only ASCII chars are allowed in headers, but we latin1 just in case
            value = value.decode("latin1")
            if corrected_name in environ:
                value = environ[corrected_name] + "," + value
            environ[corrected_name] = value
        return environ

    def start_response(self, status, response_headers, exc_info=None):
        """
        WSGI start_response callable.
        """
        # Don't allow re-calling once response has begun
        if self.response_started:
            raise exc_info[1].with_traceback(exc_info[2])
        # Don't allow re-calling without exc_info
        if hasattr(self, "response_start") and exc_info is None:
            raise ValueError(
                "You cannot call start_response a second time without exc_info"
            )
        # Extract status code
        status_code, _ = status.split(" ", 1)
        status_code = int(status_code)
        # Extract headers
        headers = [
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in response_headers
        ]
        # Build and send response start message.
        self.response_start = {
            "type": "http.response.start",
            "status": status_code,
            "headers": headers,
        }

    @sync_to_async
    def run_wsgi_app(self, message):
        """
        Called in a subthread to run the WSGI app. We encapsulate like
        this so that the start_response callable is called in the same thread.
        """
        # Translate the scope and incoming message into a WSGI environ
        environ = self.build_environ(self.scope, message)
        # Run the WSGI app
        for output in self.wsgi_application(environ, self.start_response):
            # If this is the first response, include the response headers
            if not self.response_started:
                self.response_started = True
                self.sync_send(self.response_start)
            self.sync_send(
                {"type": "http.response.body", "body": output, "more_body": True}
            )
        # Close connection
        if not self.response_started:
            self.response_started = True
            self.sync_send(self.response_start)
        self.sync_send({"type": "http.response.body"})
