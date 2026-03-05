"""
LDAP / Active Directory authentication.

Requires ldap3: pip install ldap3
Tenant must have ldap_config set:
{
  "host": "ldaps://ldap.company.com",
  "port": 636,
  "use_ssl": true,
  "base_dn": "dc=company,dc=com",
  "user_search_filter": "(sAMAccountName={username})",
  "bind_dn": "cn=svc-retrace,ou=Service Accounts,dc=company,dc=com",
  "bind_password": "service-account-password",
  "email_attribute": "mail",
  "display_name_attribute": "displayName",
  "group_attribute": "memberOf"
}
"""

from typing import Optional
import structlog

from app.core.encryption import decrypt_value

logger = structlog.get_logger()


class LDAPAuthResult:
    def __init__(self, email: str, display_name: str, groups: list[str]):
        self.email = email
        self.display_name = display_name
        self.groups = groups


async def authenticate_ldap(
    username: str,
    password: str,
    ldap_config: dict,
) -> Optional[LDAPAuthResult]:
    """Authenticate a user against LDAP/AD. Returns user info on success, None on failure."""
    try:
        import ldap3
        from ldap3 import Server, Connection, SUBTREE, Tls
        import ssl
    except ImportError:
        logger.error("ldap3 not installed — run: pip install ldap3")
        raise RuntimeError("LDAP support requires ldap3. Install with: pip install ldap3")

    host = ldap_config.get("host", "")
    port = ldap_config.get("port", 636)
    use_ssl = ldap_config.get("use_ssl", True)
    base_dn = ldap_config.get("base_dn", "")
    bind_dn = ldap_config.get("bind_dn", "")
    bind_password = decrypt_value(ldap_config.get("bind_password", ""))
    user_filter = ldap_config.get("user_search_filter", "(sAMAccountName={username})")
    email_attr = ldap_config.get("email_attribute", "mail")
    name_attr = ldap_config.get("display_name_attribute", "displayName")
    group_attr = ldap_config.get("group_attribute", "memberOf")

    tls_config = Tls(validate=ssl.CERT_NONE) if use_ssl else None
    server = Server(host, port=port, use_ssl=use_ssl, tls=tls_config, get_info=ldap3.NONE)

    # Step 1: Bind with service account to search for the user
    try:
        svc_conn = Connection(server, user=bind_dn, password=bind_password, auto_bind=True)
    except Exception as exc:
        logger.error("ldap_service_bind_failed", error=str(exc))
        return None

    search_filter = user_filter.replace("{username}", ldap3.utils.conv.escape_filter_chars(username))
    svc_conn.search(
        search_base=base_dn,
        search_filter=search_filter,
        search_scope=SUBTREE,
        attributes=[email_attr, name_attr, group_attr],
    )

    if not svc_conn.entries:
        logger.info("ldap_user_not_found", username=username)
        svc_conn.unbind()
        return None

    entry = svc_conn.entries[0]
    user_dn = entry.entry_dn
    email = str(getattr(entry, email_attr, "")) if hasattr(entry, email_attr) else ""
    display_name = str(getattr(entry, name_attr, "")) if hasattr(entry, name_attr) else username
    groups = [str(g) for g in getattr(entry, group_attr, [])] if hasattr(entry, group_attr) else []
    svc_conn.unbind()

    # Step 2: Bind as the user to verify their password
    try:
        user_conn = Connection(server, user=user_dn, password=password, auto_bind=True)
        user_conn.unbind()
    except Exception:
        logger.info("ldap_auth_failed", username=username)
        return None

    logger.info("ldap_auth_success", username=username, email=email)
    return LDAPAuthResult(email=email, display_name=display_name, groups=groups)


async def test_ldap_connection(ldap_config: dict, *, raw_password: bool = False) -> dict:
    """Test connectivity to an LDAP server using the service-account bind.
    Returns {"success": True/False, "message": "..."}.
    When *raw_password* is True the bind_password is used as-is (not decrypted).
    """
    try:
        import ldap3
        from ldap3 import Server, Connection, Tls
        import ssl
    except ImportError:
        return {"success": False, "message": "ldap3 library is not installed on the server."}

    host = ldap_config.get("host", "")
    port = ldap_config.get("port", 636)
    use_ssl = ldap_config.get("use_ssl", True)
    bind_dn = ldap_config.get("bind_dn", "")
    raw_pw = ldap_config.get("bind_password", "")
    bind_password = raw_pw if raw_password else decrypt_value(raw_pw)
    base_dn = ldap_config.get("base_dn", "")

    if not host:
        return {"success": False, "message": "LDAP host is required."}
    if not bind_dn:
        return {"success": False, "message": "Bind DN is required."}

    tls_config = Tls(validate=ssl.CERT_NONE) if use_ssl else None
    server = Server(host, port=port, use_ssl=use_ssl, tls=tls_config, get_info=ldap3.NONE)

    try:
        conn = Connection(server, user=bind_dn, password=bind_password, auto_bind=True)
    except Exception as exc:
        logger.error("ldap_test_bind_failed", error=str(exc))
        return {"success": False, "message": f"Service-account bind failed: {exc}"}

    if base_dn:
        try:
            conn.search(search_base=base_dn, search_filter="(objectClass=*)", search_scope="BASE")
        except Exception as exc:
            conn.unbind()
            return {"success": False, "message": f"Base DN lookup failed: {exc}"}

    conn.unbind()
    logger.info("ldap_test_connection_success", host=host)
    return {"success": True, "message": "Connection successful. Service-account bind and base DN verified."}


async def lookup_ldap_user(username: str, ldap_config: dict) -> Optional[LDAPAuthResult]:
    """Look up a user in LDAP without password verification. Returns user info if found, None otherwise.
    Used when adding LDAP users — validates the username exists in LDAP before creating the local record."""
    try:
        import ldap3
        from ldap3 import Server, Connection, SUBTREE, Tls
        import ssl
    except ImportError:
        logger.error("ldap3 not installed — run: pip install ldap3")
        raise RuntimeError("LDAP support requires ldap3. Install with: pip install ldap3")

    host = ldap_config.get("host", "")
    port = ldap_config.get("port", 636)
    use_ssl = ldap_config.get("use_ssl", True)
    base_dn = ldap_config.get("base_dn", "")
    bind_dn = ldap_config.get("bind_dn", "")
    bind_password = decrypt_value(ldap_config.get("bind_password", ""))
    user_filter = ldap_config.get("user_search_filter", "(sAMAccountName={username})")
    email_attr = ldap_config.get("email_attribute", "mail")
    name_attr = ldap_config.get("display_name_attribute", "displayName")
    group_attr = ldap_config.get("group_attribute", "memberOf")

    tls_config = Tls(validate=ssl.CERT_NONE) if use_ssl else None
    server = Server(host, port=port, use_ssl=use_ssl, tls=tls_config, get_info=ldap3.NONE)

    try:
        svc_conn = Connection(server, user=bind_dn, password=bind_password, auto_bind=True)
    except Exception as exc:
        logger.error("ldap_service_bind_failed", error=str(exc))
        return None

    search_filter = user_filter.replace("{username}", ldap3.utils.conv.escape_filter_chars(username))
    svc_conn.search(
        search_base=base_dn,
        search_filter=search_filter,
        search_scope=SUBTREE,
        attributes=[email_attr, name_attr, group_attr],
    )

    if not svc_conn.entries:
        logger.info("ldap_user_not_found", username=username)
        svc_conn.unbind()
        return None

    entry = svc_conn.entries[0]
    email = str(getattr(entry, email_attr, "")) if hasattr(entry, email_attr) else ""
    display_name = str(getattr(entry, name_attr, "")) if hasattr(entry, name_attr) else username
    groups = [str(g) for g in getattr(entry, group_attr, [])] if hasattr(entry, group_attr) else []
    svc_conn.unbind()

    return LDAPAuthResult(email=email, display_name=display_name, groups=groups)
