"""Resource-id prefixes, owning systems and default data categories."""

# Resource-id prefix -> owning system, used when a source does not name the system.
# Longer prefixes come first so that e.g. audit_record_ is not shadowed.
PREFIX_SYSTEM = (
    ("audit_record_", "security_service"),
    ("data_flow_", "integration_service"),
    ("email_delivery_", "email_delivery_service"),
    ("legal_hold_", "legal_service"),
    ("tombstone_", "identity_service"),
    ("analytics_", "analytics_platform"),
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("backup_", "backup_service"),
    ("marketing_", "marketing_platform"),
    ("object_", "object_storage"),
    ("cache_", "cache_service"),
)

# System -> default data category (deletion_rules.csv vocabulary). DATA_FLOW is not a
# rules category: connectors are handled as data flows that must end up disabled.
SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "export_service": "USER_EXPORT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "legal_service": "LEGAL_HOLD_RECORD",
    "marketing_platform": "MARKETING_PROFILE",
    "cache_service": "CACHE_ENTRY",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "object_storage": "SUPPORT_ATTACHMENT",
}

DATA_FLOW = "DATA_FLOW"


def system_for(resource_id: str) -> str | None:
    for prefix, system in PREFIX_SYSTEM:
        if resource_id and resource_id.startswith(prefix):
            return system
    return None


def category_from_hints(system: str | None, *hints) -> str | None:
    """Data category for a record, from its system and any table/collection/type names.

    Support records have no safe default: the evidence must say attachment or message.
    """
    h = " ".join(str(x) for x in hints if x).lower()
    if system == "support_platform" or "support" in h:
        if "attachment" in h:
            return "SUPPORT_ATTACHMENT"
        if "conversation" in h or "message" in h or "ticket" in h:
            return "SUPPORT_MESSAGE"
        return None
    if system == "analytics_platform" and "contact" in h:
        return "SECONDARY_IDENTIFIER"
    if "deleted_customer_references" in h or system == "identity_service":
        return "ACCOUNT_TOMBSTONE"
    if "retained_order" in h or "legal" in h:
        return "LEGAL_HOLD_RECORD"
    return SYSTEM_CATEGORY.get(system)
