"""Placeholders for personal data in everything sent to Claude, and the way back.

Replaced before sending:
* e-mail addresses and user principal names, anywhere in the text (EMAIL-01)
* names of people the application knows (users, document owners and approvers, evidence
  collectors, action and risk owners, display names of synced identities) when they look
  like a person's name, wherever they occur (PERSON-01)
* device names from the asset inventory (DEVICE-01)
* every value in the account lists of connector results (administrators, guests, accounts
  without MFA, stale accounts, GitHub owners), as whole values (PERSON-02)

The mapping stays in the local database. Claude's answer is translated back before anyone
reads it. Text inside evidence files that are sent as PDF or image cannot be replaced; the
Evidence form says so where an administrator allows sharing a file.
"""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
TOKEN_RE = re.compile(r"\b(?:PERSON|EMAIL|DEVICE)-\d{2,4}\b")

# Keys in connector check details whose values are accounts or people.
ACCOUNT_KEYS = {
    "not_registered",
    "admins_not_registered",
    "global_administrators",
    "guest_accounts",
    "stale_accounts",
    "admins",
    "owners",
    "members",
    "user",
    "users_list",
}
PARTICLES = {"van", "de", "der", "den", "het", "ter", "ten", "te", "le", "la", "du", "des", "von", "di", "da", "dos", "del", "d'", "op", "in", "'t"}
ROLE_WORDS = {
    "admin",
    "administrator",
    "administrators",
    "manager",
    "management",
    "officer",
    "team",
    "department",
    "board",
    "director",
    "head",
    "lead",
    "owner",
    "it",
    "security",
    "ciso",
    "cio",
    "ceo",
    "cfo",
    "cto",
    "coo",
    "dpo",
    "hr",
    "finance",
    "legal",
    "service",
    "account",
    "group",
    "support",
    "helpdesk",
    "desk",
    "operations",
    "committee",
    "council",
    "external",
    "partner",
    "provider",
    "global",
    "break",
    "glass",
    "emergency",
    "sync",
    "backup",
}


def looks_like_person(name: str) -> bool:
    """Two to five capitalised words with only letters, hyphens and apostrophes, and no role words."""
    words = (name or "").strip().split()
    if not 2 <= len(words) <= 5:
        return False
    named = 0
    for w in words:
        lw = w.lower()
        if lw.strip(".,") in ROLE_WORDS:
            return False
        if lw in PARTICLES:
            continue
        if not (w[0].isupper() and all(ch.isalpha() or ch in "-'’." for ch in w)):
            return False
        named += 1
    return named >= 2


class Pseudonymizer:
    def __init__(self) -> None:
        self.forward: dict[str, str] = {}  # original -> placeholder
        self.reverse: dict[str, str] = {}  # placeholder -> original
        self._terms: set[str] = set()  # replaced inside free text (people, devices)
        self._exact: set[str] = set()  # replaced only as a whole value (account list entries)
        self._counts: dict[str, int] = {}
        self._pattern: re.Pattern | None = None

    # ----------------------------------------------------------------- registration
    def _placeholder(self, kind: str, original: str) -> str:
        key = original.casefold() if kind == "EMAIL" else original
        if key in self.forward:
            return self.forward[key]
        n = self._counts.get(kind, 0) + 1
        self._counts[kind] = n
        token = f"{kind}-{n:02d}"
        self.forward[key] = token
        self.reverse[token] = original
        return token

    def _add_term(self, kind: str, name: str) -> None:
        self._placeholder(kind, name)
        self._terms.add(name)
        self._pattern = None

    def add_person(self, name: str | None) -> None:
        name = (name or "").strip()
        if not name:
            return
        if "@" in name:
            self._placeholder("EMAIL", name)
        elif looks_like_person(name):
            self._add_term("PERSON", name)

    def add_device(self, name: str | None) -> None:
        name = (name or "").strip()
        if len(name) >= 4 and "@" not in name:
            self._add_term("DEVICE", name)

    def add_account(self, value: str | None) -> None:
        value = (value or "").strip()
        if not value:
            return
        if "@" in value:
            self._placeholder("EMAIL", value)
            return
        self.add_person(value)
        if value not in self.forward:
            self._placeholder("PERSON", value)
        self._exact.add(value)

    # ----------------------------------------------------------------- applying
    def _term_re(self) -> re.Pattern | None:
        if self._pattern is None and self._terms:
            alts = sorted(self._terms, key=len, reverse=True)
            self._pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in alts) + r")(?!\w)")
        return self._pattern

    def text(self, value: str | None) -> str:
        if not value:
            return ""
        if value.strip() in self._exact:
            return self.forward[value.strip()]
        out = EMAIL_RE.sub(lambda m: self._placeholder("EMAIL", m.group(0)), value)
        pat = self._term_re()
        if pat is not None:
            out = pat.sub(lambda m: self.forward[m.group(0)], out)
        return out

    def obj(self, value, key: str = ""):
        """Apply to every string in a JSON-like structure; values under account keys are registered first."""
        if key in ACCOUNT_KEYS:
            self._register_accounts(value)
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.obj(v, key) for v in value]
        if isinstance(value, dict):
            return {k: self.obj(v, k) for k, v in value.items()}
        return value

    def _register_accounts(self, value) -> None:
        if isinstance(value, str):
            self.add_account(value)
        elif isinstance(value, list):
            for v in value:
                self._register_accounts(v)
        elif isinstance(value, dict):
            for k in ("user", "userPrincipalName", "login", "name", "displayName"):
                if isinstance(value.get(k), str):
                    self.add_account(value[k])

    def restore(self, value):
        """Put the original values back into Claude's answer (strings, lists, dicts)."""
        if isinstance(value, str):
            return TOKEN_RE.sub(lambda m: self.reverse.get(m.group(0), m.group(0)), value)
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        return value

    @classmethod
    def from_mapping(cls, reverse: dict[str, str]) -> Pseudonymizer:
        p = cls()
        p.reverse = dict(reverse)
        return p
