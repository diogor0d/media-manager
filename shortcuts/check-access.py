"""Read-only Access diagnostic using ignored local configuration; never follow redirects."""

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


config = json.loads((Path(__file__).parent / 'private/config.json').read_text())
base = config['baseURL']
parts = urlsplit(base)
if parts.scheme != 'https' or parts.username or parts.password or parts.path:
    raise SystemExit('Expected a configured HTTPS origin without credentials or path')
opener = build_opener(NoRedirect())
for label, headers in (
    ('anonymous', {}),
    ('service token', {
        'CF-Access-Client-Id': config['clientID'],
        'CF-Access-Client-Secret': config['clientSecret'],
    }),
):
    request = Request(base + '/v1/capabilities', headers=headers)
    try:
        response = opener.open(request, timeout=20)
    except HTTPError as error:
        response = error
    with response:
        print(f'{label}: HTTP {response.code}; type={response.headers.get_content_type()}')
        print('Cloudflare response marker:', bool(response.headers.get('CF-Ray')))
        location = response.headers.get('Location')
        if location:
            print('Redirect present (not followed; URL withheld)')
        if response.headers.get_content_type() == 'application/json':
            payload = json.loads(response.read(65536))
            print('Capabilities schema:', isinstance(payload, dict) and 'targets' in payload)
