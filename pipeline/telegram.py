"""Sending digests to a Telegram channel through the Bot API (stage `send`)."""
import httpx

API = 'https://api.telegram.org/bot{token}/{method}'


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, http: httpx.Client, token: str, chat_id: str):
        self.http, self.token, self.chat_id = http, token, chat_id

    def send(self, html: str) -> int:
        """Posts one HTML message; returns its id. Link previews are off: a digest links many sources."""
        try:
            response = self.http.post(API.format(token=self.token, method='sendMessage'), json={
                'chat_id': self.chat_id, 'text': html, 'parse_mode': 'HTML',
                'link_preview_options': {'is_disabled': True},
            })
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # httpx puts the URL, with the token in it, into its messages
            raise TelegramError(f'{type(exc).__name__}: {exc}'.replace(self.token, '<token>')) from exc
        if not body.get('ok'):
            raise TelegramError(f'{response.status_code}: {body.get("description", "no description")}')
        return body['result']['message_id']
