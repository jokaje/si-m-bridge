# Erreichbarkeit einrichten

Der Orb spricht ausschliesslich **HTTPS** und vertraut dabei nur den
Zertifizierungsstellen des Betriebssystems. Selbstsignierte Zertifikate,
reines HTTP im LAN und IP-Adressen ohne gueltiges Zertifikat funktionieren
deshalb nicht. Diese Einschraenkung ist Absicht: sie verhindert, dass jemand im
gleichen WLAN mitliest oder sich als Bridge ausgibt.

Gleichzeitig hiesse das, dass jeder Nutzer sich um Zertifikate kuemmern muesste.
Deshalb ist die Erreichbarkeit kein fester Bestandteil der Bridge, sondern eine
bewusste Entscheidung pro Installation.

## Der Wizard

```bash
python cli.py setup
```

Fragt nach dem Weg, prueft nur lokale Programme und zeigt einen
Plan. Mit Flags laesst sich das auch skripten:

```bash
python cli.py setup --exposure tailscale --hermes-home /srv/hermes-orb --profile orb
python cli.py setup --exposure caddy --domain bridge.example.com --out-dir ./generated
python cli.py setup --exposure existing --url https://bridge.example.com
python cli.py setup --exposure loopback --no-write
```

Der Wizard aendert **nichts** am System. Kein `sudo`, kein Dienstneustart, kein
Port oeffnen. Er schreibt hoechstens Proxy-Konfigurationen nach `./generated/`,
damit du sie vor dem Anwenden lesen kannst.

---

## Die Modi im Detail

### `tailscale` – nur Tailnet-Clients, nicht direkt fuer den WLAN-Orb

**Du brauchst:** Tailscale auf dem Host, angemeldet, MagicDNS und HTTPS im
Tailnet aktiviert.

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8766
tailscale serve status
```

**Vorteile:** kein Port im Router, kein DNS-Eintrag, Zertifikat erneuert sich
selbst, kein Angriffsflaeche aus dem offenen Internet.

**Nicht geraetetauglich:** Der Orb hat keinen Tailscale-Client. Ein Router oder
anderer Tailnet-Knoten im selben WLAN gibt dem Orb keinen Zugriff auf
Tailscale Serve; Subnet-Routing bewirkt die falsche Richtung. Nur als
Host/Tailnet-Test nutzen, nicht zum QR-Pairing. Fuer den Orb einen separat
geprueften, aus seinem Netz erreichbaren HTTPS-Endpunkt einrichten.

**Wenn der Wizard keinen Namen findet:** MagicDNS oder HTTPS-Zertifikate sind im
Tailnet deaktiviert. Beides laesst sich in der Tailscale-Verwaltung
einschalten. Alternativ den Namen direkt vorgeben:
`--exposure tailscale --tailnet dein-host.tail1234.ts.net`.

### `caddy` – eigene Domain, automatisches Zertifikat

**Du brauchst:** eine Domain, deren A/AAAA-Eintrag auf den Host zeigt, sowie
Port 80 und 443 aus dem Internet erreichbar (Port 80 fuer die
Zertifikatspruefung).

Der Wizard erzeugt einen `Caddyfile`:

```
bridge.example.com {
    reverse_proxy 127.0.0.1:8766
}
```

Caddy holt und erneuert das Zertifikat selbst. Das ist der wenigste Aufwand,
wenn du schon eine Domain hast.

### `nginx` – vorhandene Infrastruktur

**Du brauchst:** nginx, ein Zertifikat (z. B. per certbot) und eine Domain.

Der Wizard erzeugt einen `server`-Block mit `proxy_buffering off`. Das ist
wichtig: die Audioantwort wird gestreamt, mit Pufferung hoert man am Orb
Verzoegerungen oder gar nichts.

```bash
certbot certonly --nginx -d bridge.example.com
nginx -t && systemctl reload nginx
```

### `cloudflare` – Tunnel ohne Portfreigabe

**Du brauchst:** einen Cloudflare-Account, eine dort verwaltete Domain und
`cloudflared`.

```bash
cloudflared tunnel login
cloudflared tunnel create si-m-bridge
cloudflared tunnel route dns si-m-bridge bridge.example.com
cloudflared tunnel run si-m-bridge
```

Der Host baut die Verbindung selbst nach aussen auf. Kein Port im Router.

**Zu beachten:** Cloudflare beendet TLS am Rand. Fuer den Orb ist das
unproblematisch, weil er nur das oeffentliche Zertifikat sieht. Wenn du keine
Inhalte durch einen Dritten leiten willst, ist Tailscale die bessere Wahl.

### `existing` – du hast schon einen Proxy

Der Wizard prueft nur die URL:

```
https://bridge.example.com
```

Abgelehnt werden `http://`, Zugangsdaten in der URL, Pfade, Query-Parameter und
Loopback-Adressen. Der Proxy muss ungepufferte Antworten durchlassen.

### `loopback` – nur zum Testen

Bindet an `127.0.0.1`. Der Wizard markiert das ausdruecklich als **nicht
geraetetauglich**. Nuetzlich, um die Bridge erst einmal ohne Netzwerk zu testen.

---

## Nach dem Proxy: wirklich pruefen

Ein eingerichteter Proxy ist noch kein funktionierender Endpunkt. Teste in
dieser Reihenfolge:

```bash
# 1. Antwortet der Proxy ueberhaupt?
curl -sS -o /dev/null -w '%{http_code}\n' https://<host>/v1/inbox/meta
# erwartet: 401  (erreichbar, aber ohne Token)

# 2. Stimmt das Zertifikat fuer den Orb?
#    Der Orb prueft gegen System-CAs, also ohne -k:
curl -sS https://<host>/v1/inbox/meta

# 3. Bleibt die Verbindung fuer Audio offen?
curl -sS -X POST https://<host>/v1/turn-stream \
  -H "Authorization: Bearer <token>" \
  -H 'Content-Type: application/vnd.kim.pcm;rate=16000;channels=1;format=s16le' \
  --data-binary @probe.pcm -o antwort.bin
```

Ein `502` oder `504` heisst: der Proxy erreicht die Bridge nicht. Ein `401`
heisst: der Weg steht, es fehlt nur das Token. Erst danach koppeln.

## Haeufige Fehler

| Symptom | Ursache |
|---|---|
| `HTTPS or loopback required` | Basis-URL ist `http://` – der Orb kann das nicht |
| `Geraete-URL muss https sein` | im Wizard eine LAN-IP ohne Zertifikat angegeben |
| `502 Bad Gateway` | Bridge laeuft nicht oder bindet auf einem anderen Port |
| Orb koppelt, holt aber nichts | Ruhezeit aktiv, oder das Profil passt nicht zum Token |
| Audio kommt verzoegert | Pufferung im Proxy aktiv (`proxy_buffering off` fehlt) |
| Zertifikat abgelehnt | selbstsigniert – die Firmware kennt nur System-CAs |
| `inbox store mismatch` | `--db` zeigt nicht auf `$HERMES_HOME/sim-inbox.sqlite` |
