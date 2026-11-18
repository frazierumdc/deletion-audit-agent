"""Who is the subject, and which identifiers did they hold by a given time?

Identifiers come from the current snapshot (user_identities.csv) and the change history
(user_audit_log.csv). Each (user, identifier) pair gets the time from which it is known: a value
that appears as an audit new_value is known from that change; any other value (an old_value, or a
snapshot value never set by a recorded change) is treated as held from the start. Resolving at a
case's audit_at therefore ignores identifiers a user only acquired later.
"""

from collections import defaultdict
from dataclasses import dataclass

from deletion_audit.data import AuditChange, Identity
from deletion_audit.identifiers import identifier_tokens

ALWAYS = ""  # sorts before every timestamp


@dataclass(frozen=True)
class Subject:
    user_id: str
    identifiers: frozenset  # typed tokens (e:/p:/h:/u:) held by the cutoff
    audit_ids: tuple  # identity changes recorded by the cutoff
    matched_on: str  # the request token that resolved the subject
    ambiguous: tuple = ()  # other users holding the same token, if any


class IdentityIndex:
    def __init__(self, identities: list[Identity], audit_log: list[AuditChange]):
        set_at = {}  # (user_id, field, raw value) -> earliest change that set it
        for c in sorted(audit_log, key=lambda c: (c.event_time, c.audit_id)):
            set_at.setdefault((c.user_id, c.field_name, c.new_value), c.event_time)
        since = {}  # (user_id, token) -> known from

        def hold(uid, field_name, value):
            t = set_at.get((uid, field_name, value), ALWAYS)
            for tok in identifier_tokens(value):
                key = (uid, tok)
                since[key] = min(since.get(key, t), t)

        for ident in identities:
            since[(ident.user_id, "u:" + ident.user_id)] = ALWAYS
            for field_name in ("email", "phone", "email_hash"):
                hold(ident.user_id, field_name, getattr(ident, field_name))
        for c in audit_log:
            hold(c.user_id, c.field_name, c.old_value)
            hold(c.user_id, c.field_name, c.new_value)

        self._since = since
        self._by_token = defaultdict(list)  # token -> [(since, user_id)]
        self._by_user = defaultdict(list)  # user_id -> [(since, token)]
        for (uid, tok), t in sorted(since.items()):
            self._by_token[tok].append((t, uid))
            self._by_user[uid].append((t, tok))
        self._changes = defaultdict(list)
        for c in sorted(audit_log, key=lambda c: (c.event_time, c.audit_id)):
            self._changes[c.user_id].append(c)

    def owners(self, token: str, cutoff: str) -> set[str]:
        """Users who held this identifier at or before the cutoff."""
        return {uid for t, uid in self._by_token.get(token, ()) if t <= cutoff}

    def tokens(self, user_id: str, cutoff: str) -> frozenset:
        return frozenset(tok for t, tok in self._by_user.get(user_id, ()) if t <= cutoff)

    def changes(self, user_id: str, cutoff: str) -> list[AuditChange]:
        return [c for c in self._changes.get(user_id, ()) if c.event_time <= cutoff]

    def resolve(self, email: str, cutoff: str) -> Subject | None:
        """The user the request email belongs to (current or historical address), exact match only."""
        for tok in sorted(identifier_tokens(email), key=lambda t: not t.startswith("e:")):
            uids = sorted(self.owners(tok, cutoff))
            if uids:
                uid = uids[0]
                return Subject(
                    uid,
                    self.tokens(uid, cutoff),
                    tuple(c.audit_id for c in self.changes(uid, cutoff)),
                    tok,
                    tuple(uids[1:]),
                )
        return None
