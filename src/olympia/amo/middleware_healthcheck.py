from django.http import JsonResponse


class ALBHealthCheckMiddleware:
    """Respond to ALB health checks before Django host validation runs

    AWS ALB would send health checks with the LB node IP as the Host header
    which Django rejects via ALLOWED_HOSTS. This middleware intercepts the
    health check path and returns 200 before CommonMiddleware validates
    the Host header

    Must be placed at the top of MIDDLEWARE to run before host validation
    """

    HEALTH_CHECK_PATH = "/services/monitor.json"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == self.HEALTH_CHECK_PATH and request.method == "GET":
            return JsonResponse({"status": "ok"})
        return self.get_response(request)
