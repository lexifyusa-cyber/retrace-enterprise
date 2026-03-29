# Auth Patterns for MCP Server HTTP Clients

Reference implementations for authenticating outbound HTTP requests to target APIs.
The agent should read this file when the detected auth type is not simple Bearer/API key.

---

## 1. Bearer Token (Static)

```python
import os, httpx

API_TOKEN = os.environ["SERVICE_API_TOKEN"]

async def _request(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(
            method, path,
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
```

---

## 2. API Key — Header

```python
import os, httpx

API_KEY = os.environ["SERVICE_API_KEY"]

async def _request(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(
            method, path,
            headers={"X-API-Key": API_KEY},
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
```

---

## 3. API Key — Query Parameter

Some APIs expect the key as a URL parameter (e.g. `?api_key=xxx`).

```python
import os, httpx

API_KEY = os.environ["SERVICE_API_KEY"]

async def _request(method: str, path: str, params: dict | None = None, **kwargs) -> dict:
    params = params or {}
    params["api_key"] = API_KEY
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(method, path, params=params, **kwargs)
        resp.raise_for_status()
        return resp.json()
```

---

## 4. Basic Auth

```python
import os, httpx

USERNAME = os.environ["SERVICE_USERNAME"]
PASSWORD = os.environ["SERVICE_PASSWORD"]

async def _request(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(
            method, path,
            auth=(USERNAME, PASSWORD),
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
```

---

## 5. Multi-Header Auth

Some APIs require multiple auth headers simultaneously (e.g. `App-Id` + `App-Secret`).

```python
import os, httpx

APP_ID = os.environ["SERVICE_APP_ID"]
APP_SECRET = os.environ["SERVICE_APP_SECRET"]

async def _request(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(
            method, path,
            headers={"X-App-Id": APP_ID, "X-App-Secret": APP_SECRET},
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
```

---

## 6. OAuth2 — Client Credentials

Used by APIs that issue tokens via a token endpoint (e.g. Spotify, Microsoft Graph, Salesforce).

```python
import os, time, httpx

TOKEN_URL = os.environ["SERVICE_TOKEN_URL"]
CLIENT_ID = os.environ["SERVICE_CLIENT_ID"]
CLIENT_SECRET = os.environ["SERVICE_CLIENT_SECRET"]
SCOPES = os.environ.get("SERVICE_SCOPES", "")

_token_cache: dict = {"access_token": "", "expires_at": 0.0}

async def _get_access_token() -> str:
    if _token_cache["expires_at"] > time.time():
        return _token_cache["access_token"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": SCOPES,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        return _token_cache["access_token"]

async def _request(method: str, path: str, **kwargs) -> dict:
    token = await _get_access_token()
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(
            method, path,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
```

---

## 7. OAuth2 — Authorization Code (with Refresh Token)

Used when the user has already completed the OAuth flow and you have a refresh token.

```python
import os, time, httpx

TOKEN_URL = os.environ["SERVICE_TOKEN_URL"]
CLIENT_ID = os.environ["SERVICE_CLIENT_ID"]
CLIENT_SECRET = os.environ["SERVICE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SERVICE_REFRESH_TOKEN"]

_token_cache: dict = {"access_token": "", "expires_at": 0.0}

async def _refresh_access_token() -> str:
    if _token_cache["expires_at"] > time.time():
        return _token_cache["access_token"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        return _token_cache["access_token"]

async def _request(method: str, path: str, **kwargs) -> dict:
    token = await _refresh_access_token()
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(
            method, path,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
```

---

## 8. HMAC Signature — Generic Pattern

Used by AWS, Binance, payment gateways. **Adapt the signing recipe per API docs.**

```python
import os, time, hmac, hashlib, httpx

API_KEY = os.environ["SERVICE_API_KEY"]
API_SECRET = os.environ["SERVICE_API_SECRET"]

def _sign_request(method: str, path: str, body: str = "", params: str = "") -> dict:
    timestamp = str(int(time.time() * 1000))
    # Build the string to sign — ADAPT THIS PER API DOCS:
    # Some APIs sign: timestamp + method + path + body
    # Some APIs sign: query_string only
    # Some APIs sign: method + path + headers + body (AWS Signature V4)
    message = f"{timestamp}{method.upper()}{path}{body}"
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-API-Key": API_KEY,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }

async def _request(method: str, path: str, json_body: dict | None = None, **kwargs) -> dict:
    body_str = ""
    if json_body:
        import json as _json
        body_str = _json.dumps(json_body, separators=(",", ":"))
    auth_headers = _sign_request(method, path, body=body_str)
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(
            method, path,
            headers=auth_headers,
            json=json_body,
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
```

### HMAC Variant — Query String Signing (Binance-style)

```python
import os, time, hmac, hashlib, urllib.parse, httpx

API_KEY = os.environ["SERVICE_API_KEY"]
API_SECRET = os.environ["SERVICE_API_SECRET"]

async def _request(method: str, path: str, params: dict | None = None, **kwargs) -> dict:
    params = params or {}
    params["timestamp"] = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    params["signature"] = signature
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(
            method, path,
            params=params,
            headers={"X-MBX-APIKEY": API_KEY},
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
```

---

## 9. JWT Self-Signed (Google Cloud / Firebase style)

Used when you have a service account JSON file with a private key.

```python
import os, time, json, httpx

SERVICE_ACCOUNT_JSON = os.environ["SERVICE_ACCOUNT_JSON"]  # path to JSON file
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = os.environ.get("SERVICE_SCOPES", "")

_token_cache: dict = {"access_token": "", "expires_at": 0.0}

def _create_signed_jwt() -> str:
    import jwt  # PyJWT — add to pyproject.toml dependencies
    with open(SERVICE_ACCOUNT_JSON) as f:
        sa = json.load(f)
    now = int(time.time())
    payload = {
        "iss": sa["client_email"],
        "sub": sa["client_email"],
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
        "scope": SCOPES,
    }
    return jwt.encode(payload, sa["private_key"], algorithm="RS256")

async def _get_access_token() -> str:
    if _token_cache["expires_at"] > time.time():
        return _token_cache["access_token"]
    signed_jwt = _create_signed_jwt()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_jwt,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        return _token_cache["access_token"]

async def _request(method: str, path: str, **kwargs) -> dict:
    token = await _get_access_token()
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.request(
            method, path,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()
```

---

## 10. Cookie / Session Auth

Login once with credentials, reuse the session cookie.

```python
import os, httpx

LOGIN_URL = os.environ.get("SERVICE_LOGIN_URL", "")
USERNAME = os.environ["SERVICE_USERNAME"]
PASSWORD = os.environ["SERVICE_PASSWORD"]

_client: httpx.AsyncClient | None = None

async def _get_session() -> httpx.AsyncClient:
    global _client
    if _client is not None:
        return _client
    _client = httpx.AsyncClient(base_url=BASE_URL)
    await _client.post(
        LOGIN_URL,
        json={"username": USERNAME, "password": PASSWORD},
    )
    # Session cookie is now stored in _client.cookies
    return _client

async def _request(method: str, path: str, **kwargs) -> dict:
    client = await _get_session()
    resp = await client.request(method, path, **kwargs)
    resp.raise_for_status()
    return resp.json()
```

---

## 11. Mutual TLS (mTLS)

Client certificate authentication — used in enterprise/banking APIs.

```python
import os, httpx

CLIENT_CERT = os.environ["SERVICE_CLIENT_CERT"]  # path to .pem
CLIENT_KEY = os.environ["SERVICE_CLIENT_KEY"]     # path to .key
CA_BUNDLE = os.environ.get("SERVICE_CA_BUNDLE", "")  # optional CA cert

async def _request(method: str, path: str, **kwargs) -> dict:
    cert = (CLIENT_CERT, CLIENT_KEY)
    verify = CA_BUNDLE if CA_BUNDLE else True
    async with httpx.AsyncClient(base_url=BASE_URL, cert=cert, verify=verify) as client:
        resp = await client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json()
```

---

## Usage Notes for the Agent

- **Read the API docs** to determine which pattern applies. The `auth_details` field from analysis gives hints but may be incomplete.
- **Adapt signing recipes** — HMAC patterns vary per API. The examples above show the general structure; read the target API docs for the exact fields to sign, encoding, and header names.
- **Token caching** — Always cache OAuth2/JWT tokens. Never re-authenticate on every request.
- **Environment variables** — Use `os.environ["KEY"]` (hard fail) for required credentials, `os.environ.get("KEY", "")` for optional ones.
- **Add dependencies** — If using JWT self-signed, add `PyJWT` and `cryptography` to `pyproject.toml`.
