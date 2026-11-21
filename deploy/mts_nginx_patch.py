#!/usr/bin/env python3
import re
import sys
from pathlib import Path

CONF = Path(sys.argv[1] if len(sys.argv) > 1 else "/etc/nginx/conf.d/supraplus-miniapps.conf")
MARK = "miniapp_game_up"

MAP_BLOCK = """map $host $miniapp_game_up {
    default "";
    include /etc/nginx/miniapp-game.d/*.map;
}
"""

LOC_HEAD = """    location / {
        error_page 418 = @game;
        set $game_key "$miniapp_game_up|$uri";
        if ($game_key ~ "^[0-9.]+:[0-9]+\\|(/api/|/ws$|/healthz$)") { return 418; }
"""

GAME_LOC = """
    location @game {
        proxy_pass http://$miniapp_game_up;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $miniapp_conn;
        proxy_set_header X-Real-IP $http_cf_connecting_ip;
        proxy_set_header X-Forwarded-For $http_cf_connecting_ip;
        proxy_set_header X-Forwarded-Proto https;
        client_max_body_size 1m;
        proxy_read_timeout 120s;
    }
"""

text = CONF.read_text()
if MARK in text:
    print("already patched")
    sys.exit(0)

m = re.search(r"^server\s*\{", text, re.M)
if not m or "location / {" not in text:
    sys.exit("unexpected nginx config layout; nothing changed")
text = text[:m.start()] + MAP_BLOCK + "\n" + text[m.start():]
text = text.replace("    location / {\n", LOC_HEAD, 1)
idx = text.rstrip().rfind("}")
text = text[:idx].rstrip("\n") + "\n" + GAME_LOC + "}\n"
CONF.write_text(text)
print("patched")
