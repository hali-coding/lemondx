"""Password checks against the host's PAM stack, through ctypes.

The stdlib has no PAM module, but libpam's C API is small enough to call
directly, which keeps the backend dependency-free. Only authentication and
account checks are done -- no session is opened, since lemondx never runs
anything as the user who logged in.

Two properties of the usual ``pam_unix`` stack shape how this can be used, and
``diagnose()`` reports both at startup rather than as unexplained failures:

- A process that is not root checks passwords through the ``unix_chkpwd``
  helper, which only verifies the password of the account it runs as. A
  ``--user`` service can therefore log in exactly one person: its owner.
- That helper gets its ``/etc/shadow`` access from a setgid bit, which
  ``NoNewPrivileges=yes`` (set in the shipped systemd units) disables.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import pwd
import threading

PAM_SUCCESS = 0
PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2
PAM_ERROR_MSG = 3
PAM_TEXT_INFO = 4
PAM_CONV_ERR = 19


class PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class PamResponse(ctypes.Structure):
    # A raw pointer, not c_char_p: ctypes would otherwise convert on assignment
    # and could hand PAM a Python-owned buffer to free().
    _fields_ = [("resp", ctypes.c_void_p), ("resp_retcode", ctypes.c_int)]


CONV_FUNC = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(PamMessage)),
    ctypes.POINTER(ctypes.POINTER(PamResponse)),
    ctypes.c_void_p,
)


class PamConv(ctypes.Structure):
    _fields_ = [("conv", CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]


_libs = None
_libs_lock = threading.Lock()
# pam_unix sleeps about two seconds on a failure. Letting a few run at once
# keeps one slow attempt from queueing every other login behind it, while
# still bounding how many threads a flood of bad passwords can pin.
_slots = threading.BoundedSemaphore(4)


def _load():
    global _libs
    with _libs_lock:
        if _libs is None:
            pam_name = ctypes.util.find_library("pam")
            libc_name = ctypes.util.find_library("c")
            if not pam_name or not libc_name:
                _libs = False
            else:
                pam = ctypes.CDLL(pam_name)
                libc = ctypes.CDLL(libc_name)
                pam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                          ctypes.POINTER(PamConv), ctypes.POINTER(ctypes.c_void_p)]
                pam.pam_start.restype = ctypes.c_int
                for fn in (pam.pam_authenticate, pam.pam_acct_mgmt):
                    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
                    fn.restype = ctypes.c_int
                pam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
                pam.pam_end.restype = ctypes.c_int
                pam.pam_strerror.argtypes = [ctypes.c_void_p, ctypes.c_int]
                pam.pam_strerror.restype = ctypes.c_char_p
                libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
                libc.calloc.restype = ctypes.c_void_p
                libc.strdup.argtypes = [ctypes.c_char_p]
                libc.strdup.restype = ctypes.c_void_p
                _libs = (pam, libc)
        return _libs


def available():
    return bool(_load())


def authenticate(service, username, password):
    """``(ok, reason)``: whether PAM accepts this password and account.

    ``reason`` is PAM's own message on failure. It is for the server log, not
    for the client -- it can say whether the account exists.
    """
    libs = _load()
    if not libs:
        return False, "libpam is not available on this host"
    pam, libc = libs
    user_b = username.encode("utf-8")
    pass_b = password.encode("utf-8")

    def converse(count, messages, responses, _appdata):
        # PAM frees the array and each string with free(), so both must come
        # from libc's allocator rather than from Python.
        array = libc.calloc(count, ctypes.sizeof(PamResponse))
        if not array:
            return PAM_CONV_ERR
        responses[0] = ctypes.cast(array, ctypes.POINTER(PamResponse))
        for i in range(count):
            style = messages[i].contents.msg_style
            if style == PAM_PROMPT_ECHO_OFF:
                answer = pass_b
            elif style == PAM_PROMPT_ECHO_ON:
                answer = user_b
            else:
                continue            # informational text: nothing to answer
            copy = libc.strdup(answer)
            if not copy:
                return PAM_CONV_ERR
            responses[0][i].resp = copy
            responses[0][i].resp_retcode = 0
        return PAM_SUCCESS

    # Held in locals for the whole call: if the CFUNCTYPE wrapper were
    # collected while libpam still pointed at it, the callback would crash.
    callback = CONV_FUNC(converse)
    conv = PamConv(callback, None)
    handle = ctypes.c_void_p()

    with _slots:
        status = pam.pam_start(service.encode("utf-8"), user_b,
                               ctypes.byref(conv), ctypes.byref(handle))
        if status != PAM_SUCCESS:
            return False, "pam_start failed (%d)" % status
        try:
            status = pam.pam_authenticate(handle, 0)
            if status == PAM_SUCCESS:
                status = pam.pam_acct_mgmt(handle, 0)
            if status == PAM_SUCCESS:
                return True, ""
            reason = pam.pam_strerror(handle, status)
            return False, (reason or b"").decode("utf-8", "replace") or "error %d" % status
        finally:
            pam.pam_end(handle, status)


def diagnose(service):
    """Warnings about this process's ability to check passwords, for startup."""
    warnings = []
    if not available():
        return ["libpam was not found, so --auth pam cannot log anyone in."]
    if not os.path.exists(os.path.join("/etc/pam.d", service)):
        warnings.append(
            "/etc/pam.d/%s does not exist, so PAM uses its 'other' stack, which "
            "denies everything on some distributions. See docs/security.md for "
            "an example." % service)
    if os.geteuid() != 0 and not os.access("/etc/shadow", os.R_OK):
        try:
            me = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            me = "uid %d" % os.geteuid()
        if _no_new_privs():
            warnings.append(
                "NoNewPrivileges is set on this process, which stops PAM's "
                "unix_chkpwd helper from reading /etc/shadow: password logins "
                "will fail. Set NoNewPrivileges=no for the service (see "
                "docs/service.md).")
        else:
            warnings.append(
                "Not running as root, so PAM can only verify the password of "
                "'%s', the account lemondx runs as." % me)
    return warnings


def _no_new_privs():
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("NoNewPrivs:"):
                    return line.split()[1] == "1"
    except (OSError, IndexError):
        pass
    return False
