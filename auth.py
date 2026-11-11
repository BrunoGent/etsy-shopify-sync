import base64
import hashlib
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv, set_key

REDIRECT_URI = "http://localhost:3003/oauth/redirect"
SCOPES = "transactions_r transactions_w shops_r email_r"
ETSY_AUTH_URL = "https://www.etsy.com/oauth/connect"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
PORT = 3003


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def exchange_code_for_token(client_id: str, code: str, verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code": code,
        "code_verifier": verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(ETSY_TOKEN_URL, data=data, headers=headers)
    if not resp.ok:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()


def save_tokens(env_path: str, tokens: dict) -> None:
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in")

    if access_token:
        set_key(env_path, "ETSY_ACCESS_TOKEN", access_token)
        print("Updated ETSY_ACCESS_TOKEN in .env")
    if refresh_token:
        set_key(env_path, "ETSY_REFRESH_TOKEN", refresh_token)
        print("Updated ETSY_REFRESH_TOKEN in .env")
    if expires_in:
        expires_at = str(int(time.time() + int(expires_in)))
        set_key(env_path, "ETSY_TOKEN_EXPIRES_AT", expires_at)
        print(f"Updated ETSY_TOKEN_EXPIRES_AT in .env (expires in {expires_in}s)")


class OAuthHandler(BaseHTTPRequestHandler):
    client_id = ""
    verifier = ""
    expected_state = ""
    env_path = ""
    success = False
    error = None

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth/redirect":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        params = urllib.parse.parse_qs(parsed.query)
        if "error" in params:
            err = params["error"][0]
            OAuthHandler.error = f"OAuth error: {err}"
            self._send_html(400, f"<h1>Authorization Failed</h1><p>{err}</p>")
            return

        state = params.get("state", [None])[0]
        if state != OAuthHandler.expected_state:
            OAuthHandler.error = "State mismatch"
            self._send_html(400, "<h1>Authorization Failed</h1><p>State parameter mismatch.</p>")
            return

        code = params.get("code", [None])[0]
        if not code:
            OAuthHandler.error = "No code received"
            self._send_html(400, "<h1>Authorization Failed</h1><p>Missing code.</p>")
            return

        try:
            tokens = exchange_code_for_token(OAuthHandler.client_id, code, OAuthHandler.verifier)
            save_tokens(OAuthHandler.env_path, tokens)
            OAuthHandler.success = True
            msg = "<h1>Authentication Successful!</h1><p>Tokens saved to .env. You can close this window.</p>"
            self._send_html(200, msg)
        except Exception as e:
            OAuthHandler.error = str(e)
            self._send_html(500, f"<h1>Token Error</h1><p>{e}</p>")

    def _send_html(self, status: int, body: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = f"<html><body style='font-family:sans-serif;text-align:center;padding:40px;'>{body}</body></html>"
        self.wfile.write(html.encode("utf-8"))


def main():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(dotenv_path=env_path)

    client_id = os.getenv("ETSY_KEYSTRING")
    if not client_id:
        print(f"Error: ETSY_KEYSTRING is not set in {env_path}")
        print("Please check your .env file.")
        sys.exit(1)

    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    state = secrets.token_urlsafe(16)

    OAuthHandler.client_id = client_id
    OAuthHandler.verifier = verifier
    OAuthHandler.expected_state = state
    OAuthHandler.env_path = env_path

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{ETSY_AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    try:
        server = HTTPServer(("localhost", PORT), OAuthHandler)
    except OSError as err:
        print(f"Error starting server on port {PORT}: {err}")
        sys.exit(1)

    print(f"Starting OAuth handshake on port {PORT}...")
    print(f"Opening browser for Etsy authorization:\n{auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for callback on http://localhost:3003/oauth/redirect ...")
    try:
        while not OAuthHandler.success and not OAuthHandler.error:
            server.handle_request()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)
    finally:
        server.server_close()

    if OAuthHandler.success:
        print("\nOAuth handshake completed successfully!")
    else:
        print(f"\nHandshake failed: {OAuthHandler.error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
