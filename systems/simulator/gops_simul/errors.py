from __future__ import annotations


class SimulatorError(Exception):
    status_code = 400
    code = "simulator_error"


class BadRequest(SimulatorError):
    status_code = 400
    code = "bad_request"


class Unauthorized(SimulatorError):
    status_code = 401
    code = "unauthorized"


class Forbidden(SimulatorError):
    status_code = 403
    code = "forbidden"


class NotFound(SimulatorError):
    status_code = 404
    code = "not_found"
