"""`lemondx configure`: interactive setup that saves settings for later launches.

Each configurable area is a ``Section`` registered in ``SECTIONS``; the CLI
builds `lemondx configure <section>` from that registry, so adding one means
writing a subclass and decorating it with ``@register`` -- no parser changes.
A section owns its prompts and how it describes itself, but not its schema:
validation belongs to the module that consumes the settings (auth.py for
``auth``), so a hand-edited file and an interactive answer are held to the
same rules, and `serve` can refuse a file that breaks them.

Prompts go to stderr so `--json` output on stdout stays parseable.
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys

from . import auth, cluster, eventlog, nodesetup, pam, store
from . import fabric as fabric_net
from . import health as health_checks


class ConfigureError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# -- prompting -------------------------------------------------------------


class Prompter:
    """Question helpers that re-ask until an answer validates.

    ``read`` and ``secret`` are injectable so a section can be driven without
    a terminal, e.g. from a script feeding answers.
    """

    def __init__(self, read=input, secret=getpass.getpass, out=None):
        self._read = read
        self._secret = secret
        self._out = out or sys.stderr

    def say(self, text=""):
        self._out.write("%s\n" % text)
        self._out.flush()

    def _line(self, prompt):
        self._out.write(prompt)
        self._out.flush()
        try:
            return self._read().strip()
        except (EOFError, KeyboardInterrupt):
            self.say()
            raise ConfigureError("Cancelled; nothing was saved.", 130)

    def ask(self, question, default=None, validate=None, allow_empty=False):
        """A line of text. Empty keeps ``default``; ``validate`` may raise ValueError."""
        suffix = " [%s]" % default if default not in (None, "") else ""
        while True:
            answer = self._line("%s%s: " % (question, suffix))
            if not answer:
                if default not in (None, ""):
                    answer = default
                elif allow_empty:
                    return ""
                else:
                    self.say("  An answer is needed.")
                    continue
            if answer == "-" and allow_empty:
                return ""               # clears a default
            try:
                return validate(answer) if validate else answer
            except ValueError as exc:
                self.say("  ! %s" % exc)

    def yes_no(self, question, default):
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self._line("%s [%s]: " % (question, hint)).lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("  Answer y or n.")

    def choose_one(self, question, options, default):
        """``options`` is [(value, description)]; answer by number or value."""
        self.say(question)
        for index, (value, label) in enumerate(options, 1):
            self.say("  %d) %s" % (index, label))
        values = [value for value, _ in options]

        def pick(answer):
            if answer.isdigit() and 1 <= int(answer) <= len(values):
                return values[int(answer) - 1]
            if answer in values:
                return answer
            raise ValueError("Pick one of 1-%d." % len(values))
        return self.ask("Choice", default=default, validate=pick)

    def choose_many(self, question, options, default, none_label):
        """Several values, in the order given; ``none`` (or 0) picks none."""
        self.say(question)
        self.say("  0) %s" % none_label)
        for index, (value, label) in enumerate(options, 1):
            self.say("  %d) %s" % (index, label))
        values = [value for value, _ in options]

        def pick(answer):
            if answer.lower() in ("0", "none"):
                return []
            chosen = []
            for part in answer.replace(",", " ").split():
                if part.isdigit() and 1 <= int(part) <= len(values):
                    part = values[int(part) - 1]
                if part not in values:
                    raise ValueError("Not an option: %s" % part)
                if part not in chosen:
                    chosen.append(part)
            if not chosen:
                raise ValueError("Pick at least one, or 0 for none.")
            return chosen
        shown = ", ".join(default) if default else "none"
        answer = self.ask("Choices, separated by commas", default=shown, validate=pick)
        return answer

    def new_password(self, name, minimum=auth.MIN_PASSWORD):
        while True:
            try:
                password = self._secret("Password for %s: " % name)
                again = self._secret("Again: ")
            except (EOFError, KeyboardInterrupt):
                self.say()
                raise ConfigureError("Cancelled.", 130)
            if password != again:
                self.say("  ! Passwords do not match.")
            elif len(password) < minimum:
                self.say("  ! Use at least %d characters." % minimum)
            else:
                return password


def _comma_list(answer):
    return [part.strip() for part in answer.split(",") if part.strip()]


# -- sections --------------------------------------------------------------


class Section:
    """One `lemondx configure <name>` area, saved as store.config_path(name)."""

    name = ""
    help = ""

    def path(self):
        return store.config_path(self.name)

    def configured(self):
        return os.path.exists(self.path())

    def load(self):
        """Current settings, or None if never configured. May raise if the file is bad."""
        raise NotImplementedError

    def defaults(self):
        raise NotImplementedError

    def prompt(self, prompter, current):
        """Ask for every setting, starting from ``current``; return the new settings."""
        raise NotImplementedError

    def save(self, settings):
        """Validate and write; return what was saved."""
        raise NotImplementedError

    def describe(self, settings):
        """``[(label, text)]`` for showing settings to a person."""
        raise NotImplementedError

    def after_save(self, prompter, settings):
        """Optional follow-ups once saved, such as creating a first user."""

    def reset(self):
        return store.delete_config(self.name)


SECTIONS = {}


def register(cls):
    SECTIONS[cls.name] = cls()
    return cls


@register
class NodeSection(Section):
    """Leave unconfigured mode: what the UI's Configure node button does.

    Not a file of its own. It writes the auth, tls and listen sections through
    `nodesetup.apply()`, the same function the button calls, so the two cannot
    apply them in a different order -- the order is what keeps the port from
    ever being open without a login.
    """

    name = "node"
    help = "leave unconfigured mode: an admin login, HTTPS and where to listen, together"

    def path(self):
        return nodesetup.listen_path()

    def configured(self):
        try:
            return self.load() is not None
        except ConfigureError:
            return False

    def load(self):
        try:
            listen = nodesetup.load_listen()
            methods = (auth.load_settings() or {}).get("methods") or []
            tls = cluster.load_settings()
        except (nodesetup.SetupError, auth.AuthError, cluster.ClusterError) as exc:
            raise ConfigureError(exc.message)
        # Logins and a certificate are what leaving the mode means; the address
        # may well have been left on loopback on purpose, behind a tunnel.
        if not (methods and tls["tls_cert"]):
            return None
        return dict(self.defaults(), tls_cert=tls["tls_cert"], tls_key=tls["tls_key"],
                    auth_methods=methods, **(listen or nodesetup.DEFAULT_LISTEN))

    def defaults(self):
        return {"admin": None, "password": None, "tls_cert": None, "tls_key": None,
                "auth_methods": [], "host": nodesetup.ALL_INTERFACES,
                "port": nodesetup.DEFAULT_PORT}

    def prompt(self, p, current):
        refusal = nodesetup.cluster_refusal()
        if refusal:
            raise ConfigureError(refusal, 409)
        s = self.defaults()
        s.update(current or {})
        if not s["tls_cert"]:
            settings = cluster.load_settings()
            s["tls_cert"], s["tls_key"] = settings["tls_cert"], settings["tls_key"]
        p.say("Takes this node out of unconfigured mode: an admin login, a certificate "
              "for HTTPS, and where to listen -- what Configure node does in the UI.")
        p.say()

        admins = _admins()
        if admins:
            p.say("Admin accounts: %s" % ", ".join(admins))
            add = p.yes_no("Add another admin, or set a new password for one?", False)
        else:
            p.say("There is no admin account yet, and logins need one.")
            add = True
        if add:
            name = p.ask("Admin user name", default=auth.local_principal().name,
                         validate=_user_name)
            s["admin"], s["password"] = name, p.new_password(name)

        p.say()
        p.say("HTTPS. A node is recognised by the certificate it serves, which cluster "
              "peers pin, so a self-signed one is all federation needs.")
        s["tls_cert"], s["tls_key"] = _prompt_tls(p, s, None, default="generate")

        p.say()
        public = p.yes_no("Listen on every interface (%s), so other hosts -- and the nodes "
                          "of a cluster -- can reach it?" % nodesetup.ALL_INTERFACES,
                          not nodesetup.is_loopback(s["host"]))
        s["host"] = nodesetup.ALL_INTERFACES if public else nodesetup.DEFAULT_HOST
        return s

    def save(self, settings):
        service = auth.AuthService()
        request = {"username": settings.get("admin"), "password": settings.get("password"),
                   "tls": {"mode": "paths", "cert": settings["tls_cert"],
                           "key": settings["tls_key"]},
                   "host": settings["host"], "port": settings["port"]}
        try:
            done = nodesetup.apply(nodesetup.check(request, service, from_api=False), service)
        except (nodesetup.SetupError, auth.AuthError, cluster.ClusterError) as exc:
            raise ConfigureError("Not saved: %s" % exc.message, exc.code)
        # Never the password: this is what --json prints.
        return dict(self.defaults(), admin=done["admin"], tls_cert=done["certificate"]["path"],
                    tls_key=settings["tls_key"], auth_methods=done["auth_methods"],
                    host=done["host"], port=done["port"])

    def reset(self):
        try:
            return bool(nodesetup.reset())
        except nodesetup.SetupError as exc:
            raise ConfigureError(exc.message, exc.code)

    def describe(self, s):
        admins = _admins()
        if s.get("admin"):
            admin = "%s (new password)" % s["admin"]
        else:
            admin = ", ".join(admins) or "none -- nobody could log in"
        rows = [("admin", admin),
                ("logins", ", ".join(s.get("auth_methods") or []) or "local users")]
        if s["tls_cert"]:
            try:
                rows.append(("https", cluster.pretty_fingerprint(
                    cluster.certificate_fingerprint(s["tls_cert"]))))
            except cluster.ClusterError as exc:
                rows.append(("https", "! %s" % exc.message))
        else:
            rows.append(("https", "off"))
        rows.append(("listen", "%s:%d (%s)" % (
            s["host"], s["port"], "this host only" if nodesetup.is_loopback(s["host"])
            else "reachable from other hosts")))
        return rows

    def after_save(self, p, s):
        p.say()
        p.say("Restart `lemondx serve` to apply it (`systemctl --user restart lemondx` for "
              "the service); it then serves HTTPS on port %d and asks for a login. "
              "`lemondx configure node --reset` goes back to unconfigured mode." % s["port"])


@register
class AuthSection(Section):
    name = "auth"
    help = "login methods, PAM, trusted proxy and session settings for `serve`"

    METHOD_OPTIONS = [
        ("local", "local  - users kept by lemondx (lemondx user-add)"),
        ("pam", "pam    - accounts and passwords on this host"),
        ("proxy", "proxy  - identity from a trusted reverse proxy (SSO)"),
        ("token", "token  - API tokens only, for scripts"),
    ]

    def load(self):
        return auth.load_settings()

    def defaults(self):
        return dict(auth.DEFAULT_SETTINGS)

    def save(self, settings):
        try:
            return auth.save_settings(settings)
        except auth.AuthError as exc:
            raise ConfigureError("Not saved: %s" % exc.message, exc.code)

    def reset(self):
        return auth.reset_settings()

    def prompt(self, p, current):
        s = self.defaults()
        s.update(current or {})
        p.say("Authentication for `lemondx serve`. Enter keeps the value in [brackets]; "
              "'-' clears an optional one.")
        p.say()

        methods = p.choose_many("Which login methods should be accepted?", self.METHOD_OPTIONS,
                                default=s["methods"],
                                none_label="none   - leave authentication off")
        if "local" in methods and "pam" in methods:
            p.say()
            first = p.choose_one("A password is checked against both. Which first?", [
                ("local", "local users, then host accounts"),
                ("pam", "host accounts, then local users"),
            ], default=next(m for m in methods if m in auth.PASSWORD_METHODS))
            second = "pam" if first == "local" else "local"
            methods = [m for m in methods if m not in auth.PASSWORD_METHODS]
            methods = [first, second] + methods
        s["methods"] = methods

        if not methods:
            p.say("  Authentication stays off: anyone who can reach the port controls "
                  "your containers.")

        if any(m in auth.PASSWORD_METHODS for m in methods):
            p.say()
            s["session_hours"] = p.ask("How many hours should a login last", default=_number(
                s["session_hours"]), validate=_positive_hours)
            s["allow_insecure_login"] = p.yes_no(
                "Accept passwords over plain HTTP from other hosts? Only if TLS is "
                "impossible; loopback and trusted proxies are always allowed",
                s["allow_insecure_login"])

        if "pam" in methods:
            self._prompt_pam(p, s)
        if "proxy" in methods:
            self._prompt_proxy(p, s)

        p.say()
        if p.yes_no("Also accept a static admin token read from a file? Named API tokens "
                    "(lemondx token-create) are usually the better choice",
                    bool(s["token_file"])):
            s["token_file"] = p.ask("Token file", default=s["token_file"],
                                    validate=lambda answer: _token_file(p, answer))
        else:
            s["token_file"] = None
        return s

    def _prompt_pam(self, p, s):
        p.say()
        p.say("PAM")
        s["pam_service"] = p.ask("PAM service name (/etc/pam.d/<name>)", default=s["pam_service"])
        for warning in pam.diagnose(s["pam_service"]):
            p.say("  ! %s" % warning)
        detected = auth.default_pam_admin_groups()
        admin = s["pam_admin_groups"] if s["pam_admin_groups"] is not None else detected
        answer = p.ask("Groups whose members get admin, comma separated",
                       default=", ".join(admin), allow_empty=True, validate=_groups(p))
        # Kept as "follow the daemon's group" while it matches, so the file
        # stays right if this host moves from LXD to Incus.
        s["pam_admin_groups"] = None if answer == detected and s["pam_admin_groups"] is None \
            else (answer or [])
        s["pam_operator_groups"] = p.ask(
            "Groups whose members get operator access (run, stop and destroy; change no "
            "settings)", default=", ".join(s["pam_operator_groups"]),
            allow_empty=True, validate=_groups(p)) or []
        s["pam_read_groups"] = p.ask("Groups whose members get read-only access",
                                     default=", ".join(s["pam_read_groups"]),
                                     allow_empty=True, validate=_groups(p)) or []
        if not (s["pam_admin_groups"] if s["pam_admin_groups"] is not None else detected) \
                and not s["pam_operator_groups"] and not s["pam_read_groups"]:
            p.say("  ! With no groups, PAM will refuse every login.")

    def _prompt_proxy(self, p, s):
        p.say()
        p.say("Trusted proxy -- only these addresses may vouch for a user, so keep lemondx "
              "reachable through the proxy alone.")
        s["trusted_proxies"] = p.ask(
            "Proxy addresses (IPs or CIDRs, comma separated)",
            default=", ".join(s["trusted_proxies"]) or None, validate=_networks)
        s["proxy_user_header"] = p.ask("Header carrying the user name",
                                       default=s["proxy_user_header"])
        groups_header = p.ask("Header carrying the user's groups (blank: every proxied user "
                              "is an admin)", default=s["proxy_groups_header"], allow_empty=True)
        s["proxy_groups_header"] = groups_header or None
        if groups_header:
            s["proxy_admin_group"] = p.ask("Group granted admin", default=s["proxy_admin_group"],
                                           allow_empty=True) or None
            s["proxy_operator_group"] = p.ask("Group granted operator access",
                                              default=s["proxy_operator_group"],
                                              allow_empty=True) or None
            s["proxy_read_group"] = p.ask("Group granted read-only access",
                                          default=s["proxy_read_group"], allow_empty=True) or None
        else:
            s["proxy_admin_group"] = s["proxy_operator_group"] = s["proxy_read_group"] = None

    def after_save(self, p, s):
        service = auth.AuthService()
        if "local" in s["methods"] and not service.list_users():
            p.say()
            if p.yes_no("There are no local users yet, so nobody could log in. Create an "
                        "admin now?", True):
                name = p.ask("User name", default=auth.local_principal().name)
                try:
                    service.set_user(name, password=p.new_password(name), role=auth.ADMIN)
                except auth.AuthError as exc:
                    p.say("  ! %s Add one later with `lemondx user-add`." % exc.message)
                else:
                    p.say("  + added admin user %s" % name)
        if "token" in s["methods"] and not service.list_tokens(auth.local_principal()):
            p.say()
            if p.yes_no("Create an API token now?", False):
                name = p.ask("Token name", default="cli")
                role = p.choose_one("Access", [(auth.ADMIN, "admin"),
                                               (auth.OPERATOR, "operator"),
                                               (auth.READ, "read-only")], default=auth.ADMIN)
                days = p.ask("Expires after (e.g. 30d, 12h; blank for never)", allow_empty=True,
                             validate=_duration)
                try:
                    token = service.create_token(auth.local_principal(), name, role=role,
                                                 expires_days=days or None)
                except auth.AuthError as exc:
                    p.say("  ! %s" % exc.message)
                else:
                    p.say("  + created token %s -- shown once:" % name)
                    p.say()
                    p.say("    %s" % token["token"])
        p.say()
        p.say("This applies the next time `lemondx serve` starts (restart the service if one "
              "is running). Flags given to `serve` override individual settings; "
              "--ignore-config skips the file.")

    def describe(self, s):
        methods = s["methods"]
        rows = [("methods", ", ".join(methods) or "none (authentication off)")]
        if any(m in auth.PASSWORD_METHODS for m in methods):
            rows.append(("session hours", _number(s["session_hours"])))
            rows.append(("insecure login", "allowed" if s["allow_insecure_login"] else "refused"))
        if "pam" in methods:
            rows.append(("pam service", s["pam_service"]))
            rows.append(("pam admin groups", ", ".join(s["pam_admin_groups"])
                         if s["pam_admin_groups"] is not None
                         else "the daemon's group (%s)"
                         % (", ".join(auth.default_pam_admin_groups()) or "none on this host")))
            rows.append(("pam operator groups", ", ".join(s["pam_operator_groups"]) or "none"))
            rows.append(("pam read groups", ", ".join(s["pam_read_groups"]) or "none"))
        if "proxy" in methods:
            rows.append(("trusted proxies", ", ".join(s["trusted_proxies"])))
            rows.append(("proxy user header", s["proxy_user_header"]))
            if s["proxy_groups_header"]:
                rows.append(("proxy groups header", s["proxy_groups_header"]))
                rows.append(("proxy admin group", s["proxy_admin_group"] or "none"))
                rows.append(("proxy operator group", s["proxy_operator_group"] or "none"))
                rows.append(("proxy read group", s["proxy_read_group"] or "none"))
        rows.append(("static token file", s["token_file"] or "none"))
        return rows


@register
class HealthSection(Section):
    name = "health"
    help = "how often `serve` checks running instances, and what counts as degraded"

    def load(self):
        try:
            raw = store.load_config(self.name)
            return None if raw is None else health_checks.clean_settings(raw)
        except (ValueError, health_checks.HealthSettingsError) as exc:
            raise ConfigureError(getattr(exc, "message", str(exc)))

    def defaults(self):
        return dict(health_checks.DEFAULT_SETTINGS)

    def save(self, settings):
        try:
            return health_checks.save_settings(settings)
        except health_checks.HealthSettingsError as exc:
            raise ConfigureError("Not saved: %s" % exc.message)

    def reset(self):
        return health_checks.reset_settings()

    def prompt(self, p, current):
        s = self.defaults()
        s.update(current or {})
        p.say("Health checks run inside `lemondx serve`: every interval, each running "
              "instance is probed and its CPU and memory compared with the limits below.")
        p.say()
        s["enabled"] = p.yes_no("Run health checks?", s["enabled"])
        if not s["enabled"]:
            return s
        ranges = health_checks._RANGES

        def number(key, question):
            low, high, integer = ranges[key]

            def validate(answer):
                try:
                    value = float(answer)
                except ValueError:
                    raise ValueError("Enter a number.")
                if integer and not value.is_integer():
                    raise ValueError("Enter a whole number.")
                if not low <= value <= high:
                    raise ValueError("Enter a value from %g to %g." % (low, high))
                return int(value) if integer or value.is_integer() else value
            s[key] = p.ask(question, default=_number(s[key]), validate=validate)

        number("interval_seconds", "Seconds between checks")
        p.say()
        p.say("Degraded when a running instance is over any of these:")
        number("cpu_percent", "  CPU, as a percent of its CPUs over the interval")
        number("memory_percent", "  Memory, as a percent of its limit (no limit: not judged)")
        number("load_average", "  Load average over one minute (runnable tasks, not scaled by CPUs)")
        p.say()
        p.say("Unhealthy when it stops answering:")
        number("probe_timeout_seconds", "  Seconds to wait for an answer")
        number("failures_before_unhealthy", "  Missed answers in a row before it counts")
        number("start_grace_seconds", "  Seconds after starting before any of this applies")
        return s

    def describe(self, s):
        if not s["enabled"]:
            return [("checks", "off")]
        return [
            ("checks", "every %ss" % _number(s["interval_seconds"])),
            ("degraded at", "CPU %s%%, memory %s%%, load average over %s" % (
                _number(s["cpu_percent"]), _number(s["memory_percent"]),
                _number(s["load_average"]))),
            ("unhealthy after", "%d missed probe%s (%ss timeout)" % (
                s["failures_before_unhealthy"], "" if s["failures_before_unhealthy"] == 1 else "s",
                _number(s["probe_timeout_seconds"]))),
            ("start grace", "%ss" % _number(s["start_grace_seconds"])),
        ]

    def after_save(self, p, s):
        p.say()
        p.say("This applies the next time `lemondx serve` starts; `lemondx health` uses it "
              "straight away.")


_LEVEL_CHOICES = [
    ("debug", "debug -- also every read and request: a lot"),
    ("info", "info -- every change, and who made it (recommended)"),
    ("notice", "notice -- logins, users, tokens, membership and settings only"),
    ("warning", "warning -- only what was refused or failed"),
    ("error", "error -- only lemondx's own faults"),
]


@register
class LoggingSection(Section):
    """The cluster's logging settings: one shared record, not a config/ file.

    Saved through ClusterService like the Cluster tab saves it, so a change
    made here reaches every member too.
    """
    name = "logging"
    help = "log level, and a remote syslog host -- for every member of the cluster"

    def path(self):
        return store.settings_record_path(eventlog.SETTING_NAME) or os.path.join(
            store.settings_records_dir(), "%s.json" % eventlog.SETTING_NAME)

    def configured(self):
        return eventlog.SETTING_NAME in store.load_settings_records()

    def load(self):
        record = store.load_settings_records().get(eventlog.SETTING_NAME)
        if record is None:
            return None
        try:
            return eventlog.clean_settings(record["value"])
        except eventlog.LoggingSettingsError as exc:
            raise ConfigureError(exc.message)

    def defaults(self):
        return eventlog.clean_settings(eventlog.DEFAULT_SETTINGS)

    @staticmethod
    def _cluster():
        # Imported here: service.py is heavy, and only this section needs it.
        from .lxd import LXDError
        from .service import ContainerService
        try:
            service = ContainerService()
        except LXDError as exc:
            raise ConfigureError("Cannot reach the daemon to share the setting: %s" % exc)
        return cluster.ClusterService(service, auth.AuthService(
            auth.build_config(auth.load_settings())))

    def save(self, settings):
        try:
            return self._cluster().save_setting(eventlog.SETTING_NAME, settings)["value"]
        except cluster.ClusterError as exc:
            raise ConfigureError("Not saved: %s" % exc.message)

    def reset(self):
        if not self.configured():
            return False
        self._cluster().delete_setting(eventlog.SETTING_NAME)
        return True

    def prompt(self, p, current):
        s = self.defaults()
        s.update(current or {})
        p.say("Everything lemondx changes is logged to this host's syslog (the journal "
              "under systemd), and optionally to a remote syslog host as well.")
        p.say()
        s["level"] = p.choose_one("Lowest severity to log", _LEVEL_CHOICES, s["level"])
        s["local"] = {"facility": p.choose_one("Facility for the local log",
                                               [(f, f) for f in eventlog.FACILITIES],
                                               s["local"]["facility"])}
        remote = next((d for d in s["destinations"] if d["type"] == "syslog"), None)
        p.say()
        if not p.yes_no("Also send to a remote syslog host?", remote is not None):
            s["destinations"] = []
            return s
        remote = dict(remote or {"type": "syslog", "port": 514, "protocol": "udp",
                                 "facility": "local0", "enabled": True})

        def host(answer):
            return eventlog.SyslogDestinationType.clean(dict(remote, host=answer))["host"]
        remote["host"] = p.ask("Remote host (name or address)", default=remote.get("host"),
                               validate=host)
        remote["port"] = int(p.ask("Port", default=str(remote["port"]), validate=_port))
        remote["protocol"] = p.choose_one("Protocol", [("udp", "udp"), ("tcp", "tcp")],
                                          remote["protocol"])
        remote["facility"] = p.choose_one("Facility at the remote host",
                                          [(f, f) for f in eventlog.FACILITIES],
                                          remote["facility"])
        remote["enabled"] = True
        s["destinations"] = [remote]
        return s

    def describe(self, s):
        rows = [("level", s["level"]),
                ("local", "%s, facility %s" % (eventlog.LOCAL_SOCKET, s["local"]["facility"]))]
        for d in s["destinations"]:
            rows.append(("remote", "%s %s:%d, facility %s%s" % (
                d["protocol"], d["host"], d["port"], d["facility"],
                "" if d["enabled"] else " (off)")))
        if not s["destinations"]:
            rows.append(("remote", "none"))
        return rows

    def after_save(self, p, s):
        p.say()
        p.say("Saved for the whole cluster: every member that could be reached has it "
              "now, and a running `serve` uses it straight away.")


@register
class ClusterSection(Section):
    name = "cluster"
    help = "this node's name, address and TLS certificate for federating with others"

    def load(self):
        try:
            raw = store.load_config(self.name)
            return None if raw is None else cluster.clean_settings(raw)
        except (ValueError, cluster.ClusterError) as exc:
            raise ConfigureError(getattr(exc, "message", str(exc)))

    def defaults(self):
        return dict(cluster.DEFAULT_SETTINGS)

    def save(self, settings):
        try:
            return cluster.save_settings(settings)
        except cluster.ClusterError as exc:
            raise ConfigureError("Not saved: %s" % exc.message, exc.code)

    def reset(self):
        return cluster.reset_settings()

    def prompt(self, p, current):
        s = self.defaults()
        s.update(current or {})
        p.say("Federation settings. None of these is required: joining a cluster "
              "works them out and saves what it chose. Set them to override that "
              "-- a DNS name, an address behind NAT, a certificate of your own.")
        p.say()

        s["name"] = p.ask("What other nodes should call this one",
                          default=s["name"] or cluster.default_node_name(),
                          validate=_node_name)
        s["url"] = p.ask("Address peers reach it on (blank: work it out each time)",
                         default=s["url"] or _default_node_url(),
                         allow_empty=True, validate=_node_url) or ""

        p.say()
        p.say("A node is recognised by the TLS certificate it serves, which peers pin "
              "when they join -- so it has to serve HTTPS. One is generated on first "
              "use if you have none.")
        s["tls_cert"], s["tls_key"] = _prompt_tls(
            p, s, "decide later -- one is generated when this node federates")

        p.say()
        s["allow_enrollment"] = p.yes_no(
            "Let nodes join this cluster through this node, with a valid join code?",
            s["allow_enrollment"])
        return s

    def describe(self, s):
        rows = [("node name", s["name"] or "%s (this host)" % cluster.default_node_name()),
                ("address", s["url"] or "worked out when federating")]
        if s["tls_cert"]:
            try:
                rows.append(("fingerprint", cluster.pretty_fingerprint(
                    cluster.certificate_fingerprint(s["tls_cert"]))))
            except cluster.ClusterError as exc:
                rows.append(("fingerprint", "! %s" % exc.message))
            rows.append(("certificate", s["tls_cert"]))
        else:
            rows.append(("certificate", "generated when federating"))
        rows.append(("joining through this node",
                     "allowed" if s["allow_enrollment"] else "refused"))
        return rows

    def after_save(self, p, s):
        p.say()
        if s["tls_cert"]:
            p.say("`lemondx serve` uses this certificate unless --tls-cert or --no-tls "
                  "says otherwise.")
        p.say("`lemondx cluster invite` starts a cluster here and prints a code; "
              "`lemondx cluster join <code>` joins one. Neither needs anything else "
              "set up first.")


@register
class TlsSection(Section):
    """HTTPS for `serve`, without federating.

    Deliberately not a file of its own: it edits the certificate the cluster
    section names, which is what `serve` already defaults to. A node serves one
    certificate and its peers pin that one, so two saved answers to "which
    certificate" could only ever disagree.
    """

    name = "tls"
    help = "serve HTTPS: the certificate `lemondx serve` uses by default"

    def path(self):
        return store.config_path(cluster.SETTINGS_SECTION)

    def _cluster(self):
        try:
            raw = store.load_config(cluster.SETTINGS_SECTION)
            return cluster.clean_settings(raw) if raw is not None else None
        except (ValueError, cluster.ClusterError) as exc:
            raise ConfigureError(getattr(exc, "message", str(exc)))

    def configured(self):
        try:
            return bool((self._cluster() or {}).get("tls_cert"))
        except ConfigureError:
            return False

    def load(self):
        current = self._cluster()
        if not current or not current["tls_cert"]:
            return None
        return {"tls_cert": current["tls_cert"], "tls_key": current["tls_key"]}

    def defaults(self):
        return {"tls_cert": None, "tls_key": None}

    def prompt(self, p, current):
        s = self.defaults()
        s.update(current or {})
        p.say("The certificate `lemondx serve` uses for HTTPS unless told otherwise "
              "(--tls-cert, or --no-tls behind a proxy that terminates TLS).")
        if nodesetup.in_cluster():
            p.say("! This node is in a cluster, and its peers pin the certificate it "
                  "serves: replacing it cuts them off until each is told the new "
                  "fingerprint.")
        p.say()
        s["tls_cert"], s["tls_key"] = _prompt_tls(
            p, s, None if nodesetup.in_cluster() else "no certificate -- serve plain HTTP",
            default="generate")
        return s

    def save(self, settings):
        if not settings.get("tls_cert") and nodesetup.in_cluster():
            raise ConfigureError("Not saved: this node is in a cluster, whose members "
                                 "only speak HTTPS to it.", 409)
        merged = self._cluster() or dict(cluster.DEFAULT_SETTINGS)
        merged.update(tls_cert=settings.get("tls_cert"), tls_key=settings.get("tls_key"))
        try:
            saved = cluster.save_settings(merged)
        except cluster.ClusterError as exc:
            raise ConfigureError("Not saved: %s" % exc.message, exc.code)
        return {"tls_cert": saved["tls_cert"], "tls_key": saved["tls_key"]}

    def reset(self):
        current = self._cluster()
        if not current or not current["tls_cert"]:
            return False
        if nodesetup.in_cluster():
            raise ConfigureError(
                "This node is in a cluster, whose members only speak HTTPS to it; "
                "its certificate stays. `lemondx configure tls` replaces it.", 409)
        cluster.save_settings(dict(current, tls_cert=None, tls_key=None))
        return True

    def describe(self, s):
        if not s["tls_cert"]:
            return [("https", "off -- `lemondx serve` serves plain HTTP")]
        rows = [("https", "on by default")]
        try:
            rows.append(("fingerprint", cluster.pretty_fingerprint(
                cluster.certificate_fingerprint(s["tls_cert"]))))
        except cluster.ClusterError as exc:
            rows.append(("fingerprint", "! %s" % exc.message))
        rows += [("certificate", s["tls_cert"]), ("key", s["tls_key"] or "")]
        return rows

    def after_save(self, p, s):
        p.say()
        if s["tls_cert"]:
            p.say("`lemondx serve` serves HTTPS with this certificate from its next "
                  "start; --no-tls serves plain HTTP for one run. A self-signed "
                  "certificate makes browsers warn once -- compare the fingerprint.")
        else:
            p.say("`lemondx serve` serves plain HTTP.")


@register
class ListenSection(Section):
    name = "listen"
    help = "the address and port `lemondx serve` listens on"

    def load(self):
        try:
            return nodesetup.load_listen()
        except nodesetup.SetupError as exc:
            raise ConfigureError(exc.message)

    def defaults(self):
        return dict(nodesetup.DEFAULT_LISTEN)

    def save(self, settings):
        try:
            return nodesetup.save_listen(settings)
        except nodesetup.SetupError as exc:
            raise ConfigureError("Not saved: %s" % exc.message, exc.code)

    def reset(self):
        return nodesetup.reset_listen()

    def prompt(self, p, current):
        s = self.defaults()
        s.update(current or {})
        p.say("Where `lemondx serve` listens: %s for every interface, %s for this host "
              "only. --host and --port override it for one run."
              % (nodesetup.ALL_INTERFACES, nodesetup.DEFAULT_HOST))
        if nodesetup.in_cluster():
            p.say("! This node is in a cluster, whose members call it at the address it "
                  "advertises: keep it reachable there.")
        p.say()
        s["host"] = p.ask("Address to listen on", default=s["host"], validate=_listen_host)
        s["port"] = p.ask("Port", default=str(s["port"]), validate=_port)
        if not nodesetup.is_loopback(s["host"]) and not _logins_on():
            p.say("  ! Logins are off, so anyone who reaches this port controls your "
                  "containers. `lemondx configure node` sets up a login and HTTPS "
                  "along with the address.")
        return s

    def describe(self, s):
        where = "this host only" if nodesetup.is_loopback(s["host"]) else \
            "every interface" if s["host"] == nodesetup.ALL_INTERFACES else "one interface"
        return [("address", "%s (%s)" % (s["host"], where)), ("port", str(s["port"]))]

    def after_save(self, p, s):
        p.say()
        p.say("This applies the next time `lemondx serve` starts without --host or --port "
              "(a systemd unit installed before this setting existed passes both: drop "
              "them from its ExecStart).")


@register
class FabricSection(Section):
    name = "fabric"
    help = "routed, non-NAT networking between the containers of a cluster"

    def load(self):
        try:
            raw = store.load_config(self.name)
            return None if raw is None else fabric_net.clean_settings(raw)
        except (ValueError, fabric_net.FabricError) as exc:
            raise ConfigureError(getattr(exc, "message", str(exc)))

    def defaults(self):
        return {"via": "", "fabrics": {}, "lan": [], "admit": []}

    def save(self, settings):
        try:
            return fabric_net.save_settings(settings)
        except fabric_net.FabricError as exc:
            raise ConfigureError("Not saved: %s" % exc.message, exc.code)

    def reset(self):
        return fabric_net.reset_settings()

    def prompt(self, p, current):
        s = self.defaults()
        s.update(current or {})
        p.say("Fabrics give each node a subnet of its own in a shared prefix and "
              "route between the hosts, so a container can reach one on another "
              "node directly. They are created for the whole cluster with "
              "`lemondx fabric create` or from the Network tab; the one thing "
              "set per node is the address peers route to.")
        p.say()
        s["via"] = p.ask(
            "Address peers route this node's containers to (blank: work it out)",
            default=s["via"], allow_empty=True, validate=_optional_ip) or ""
        # The fabrics themselves are not asked for: each one is a subnet the
        # cluster allocated, and taking one by hand is how two nodes end up on
        # the same range. They are kept as they were.
        return s

    def describe(self, s):
        rows = [("this node routes to",
                 s["via"] or "the address its cluster URL names")]
        for name, fabric in sorted((s.get("fabrics") or {}).items()):
            rows.append(("fabric %s" % name, "%s in %s%s" % (
                fabric["subnet"], fabric["prefix"], "" if fabric["nat"] else ", no NAT")))
        if not s.get("fabrics"):
            rows.append(("fabrics", "none on this node"))
        return rows

    def after_save(self, p, s):
        p.say()
        p.say("`lemondx fabric list` shows every fabric in the cluster; "
              "`lemondx fabric status` what is missing on this node.")


# -- validators ------------------------------------------------------------


def _number(value):
    return ("%g" % value) if isinstance(value, (int, float)) else str(value)


def _node_name(answer):
    if not cluster.NODE_NAME.match(answer):
        raise ValueError("Use letters, digits and . _ -, up to 64 characters.")
    return answer


def _node_url(answer):
    try:
        return cluster.normalize_url(answer)
    except cluster.NodeError as exc:
        raise ValueError(exc.message)


def _prompt_tls(p, s, none_label, default="none"):
    """``(cert, key)`` from generating, naming or (given ``none_label``) skipping one."""
    if s["tls_cert"] and os.path.exists(s["tls_cert"]):
        p.say("  Currently %s" % s["tls_cert"])
        if not p.yes_no("Replace it?", False):
            return s["tls_cert"], s["tls_key"]
    elif s["tls_cert"]:
        p.say("  ! %s no longer exists." % s["tls_cert"])

    choices = [("generate", "generate a self-signed one now (needs openssl)"),
               ("existing", "use a certificate I already have")]
    if none_label:
        choices.append(("none", none_label))
    choice = p.choose_one("Certificate", choices, default=default)
    if choice == "none":
        return None, None
    if choice == "existing":
        cert = p.ask("Certificate file (PEM)", default=s["tls_cert"],
                     validate=_readable_file)
        key = p.ask("Private key file (PEM)", default=s["tls_key"],
                    validate=_readable_file)
        try:
            p.say("  Fingerprint %s"
                  % cluster.pretty_fingerprint(cluster.certificate_fingerprint(cert)))
        except cluster.ClusterError as exc:
            p.say("  ! %s" % exc.message)
        return cert, key

    host = _host_of(s.get("url") or "") or cluster.guess_local_address() or "localhost"
    host = p.ask("Address to name in the certificate", default=host)
    try:
        made = cluster.generate_certificate(host)
    except cluster.ClusterError as exc:
        raise ConfigureError(exc.message, exc.code)
    p.say("  + wrote %s" % made["cert"])
    p.say("  Fingerprint %s" % cluster.pretty_fingerprint(made["fingerprint"]))
    return made["cert"], made["key"]


def _logins_on():
    try:
        return bool((auth.load_settings() or {}).get("methods"))
    except auth.AuthError:
        return False


def _admins():
    return [u["name"] for u in auth.AuthService().list_users() if u["role"] == auth.ADMIN]


def _user_name(answer):
    try:
        return auth.check_user_name(answer)
    except auth.AuthError as exc:
        raise ValueError(exc.message)


def _listen_host(answer):
    try:
        return nodesetup.clean_listen({"host": answer})["host"]
    except nodesetup.SetupError as exc:
        raise ValueError(exc.message)


def _port(answer):
    try:
        port = int(answer)
    except ValueError:
        raise ValueError("Enter a port number.")
    try:
        return nodesetup.clean_listen({"port": port})["port"]
    except nodesetup.SetupError as exc:
        raise ValueError(exc.message)


def _default_node_url():
    address = cluster.guess_local_address()
    return "https://%s:8099" % address if address else ""


def _host_of(url):
    try:
        return cluster.parse_url(url)[1]
    except cluster.NodeError:
        return ""


def _readable_file(answer):
    path = os.path.abspath(os.path.expanduser(answer))
    if not os.path.isfile(path):
        raise ValueError("No such file: %s" % path)
    if not os.access(path, os.R_OK):
        raise ValueError("Cannot read %s." % path)
    return path


def _positive_hours(answer):
    try:
        hours = float(answer)
    except ValueError:
        raise ValueError("Enter a number of hours.")
    if not 0 < hours <= 24 * 366:
        raise ValueError("Enter a number of hours above 0.")
    return int(hours) if hours.is_integer() else hours


def _groups(prompter):
    def validate(answer):
        groups = _comma_list(answer)
        missing = [g for g in groups if not auth._group_exists(g)]
        if missing:
            # Allowed -- the group may be created later -- but worth saying.
            prompter.say("  ! No such group on this host yet: %s" % ", ".join(missing))
        return groups
    return validate


def _networks(answer):
    proxies = _comma_list(answer)
    if not proxies:
        raise ValueError("At least one address is needed.")
    for proxy in proxies:
        try:
            auth.ipaddress.ip_network(proxy, strict=False)
        except ValueError:
            raise ValueError("Not an IP address or network: %s" % proxy)
    return proxies


def _duration(answer):
    try:
        return auth.parse_duration_days(answer) if answer else None
    except auth.AuthError as exc:
        raise ValueError(exc.message)


def _token_file(prompter, answer):
    path = os.path.abspath(os.path.expanduser(answer))
    if os.path.exists(path):
        if not os.access(path, os.R_OK):
            raise ValueError("Cannot read %s." % path)
        try:
            auth.read_token_file(path)
        except auth.AuthError as exc:
            raise ValueError(exc.message)
        return path
    if not prompter.yes_no("%s does not exist. Create it with a new random token?" % path, True):
        raise ValueError("Give a file that exists, or let it be created.")
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # O_EXCL and 0600 from the start: the token is never readable by others,
    # not even between creating the file and tightening its mode.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(secrets.token_urlsafe(32) + "\n")
    prompter.say("  + wrote a new token to %s" % path)
    return path


def _optional_ip(answer):
    import ipaddress
    try:
        return str(ipaddress.ip_address((answer or "").strip()))
    except ValueError:
        raise ValueError("Enter an IP address.")
