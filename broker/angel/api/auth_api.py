import json

import httpx

from utils.config import get_broker_api_key
from utils.httpx_client import get_httpx_client


def authenticate_broker(clientcode, broker_pin, totp_code, account_id=None):
    """
    Authenticate with the broker and return the auth token.
    """
    api_key = get_broker_api_key(account_id, broker="angel")
    if not api_key:
        return (
            None,
            None,
            "Angel API key not configured for this account. Set it in Profile > Accounts.",
        )

    try:
        # Get the shared httpx client
        client = get_httpx_client()

        payload = json.dumps({"clientcode": clientcode, "password": broker_pin, "totp": totp_code})
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "CLIENT_LOCAL_IP",  # Ensure these are handled or replaced appropriately
            "X-ClientPublicIP": "CLIENT_PUBLIC_IP",
            "X-MACAddress": "MAC_ADDRESS",
            "X-PrivateKey": api_key,
        }

        response = client.post(
            "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword",
            headers=headers,
            content=payload,
        )

        # Add status attribute for compatibility with the existing codebase
        response.status = response.status_code

        data = response.text
        data_dict = json.loads(data)

        # A refused login comes back with "data": null plus Angel's own
        # message/errorcode (e.g. AG8004 Invalid API Key, invalid TOTP), so
        # check the type before looking inside it.
        login_data = data_dict.get("data")
        if isinstance(login_data, dict) and login_data.get("jwtToken"):
            # Return both JWT token and feed token if available (None if not)
            return login_data["jwtToken"], login_data.get("feedToken"), None

        message = data_dict.get("message") or "Authentication failed. Please try again."
        errorcode = data_dict.get("errorcode") or data_dict.get("errorCode")
        return None, None, f"{message} ({errorcode})" if errorcode else message
    except Exception as e:
        return None, None, str(e)
