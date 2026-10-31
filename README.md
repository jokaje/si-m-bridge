# SI-M Bridge

Verbindet einen **Hermes-Agenten** mit einem **SI-M Orb**. Der Orb zeigt
Benachrichtigungen an, wenn dein Agent eine Aufgabe erledigt hat oder eine
Rückfrage offen ist – und du redest per Sprache mit genau dem Agenten, der am
Orb eingestellt ist.

> *English summary: A self-hosted bridge between a Hermes agent and the SI-M Orb.
> It exposes a small authenticated HTTPS API, queues explicit notifications in a
> SQLite inbox, and runs agent turns inside a fixed, private Hermes profile. You
> choose how the endpoint becomes reachable. Wire contract: [docs/PROTOCOL.md](docs/PROTOCOL.md).*

---

## Was die Bridge macht

```
   ┌──────────────┐        sim_notify        ┌──────────────┐
   │    Hermes    │  ──────────────────────▶ │   SQLite     │
   │  (dein Agent)│   explizites Werkzeug    │    Inbox     │
   └──────┬───────┘                          └──────┬───────┘
          │                                         │
          │ Agentenrunde im festen Profil           │ HTTPS (Bearer)
          ▼                                         ▼
   ┌──────────────────────────────────────────────────────┐
   │                     SI-M Bridge                      │
   │  /v1/turn  /v1/turn-stream  /v1/inbox  /v1/pair/*    │
   └──────────────────────────┬───────────────────────────┘
                              │ dein gewaehlter Weg
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
          Tailscale      Caddy/nginx     Cloudflare
                              │
                              ▼
                        ┌───────────┐
                        │  SI-M Orb │
                        └───────────┘
```

Drei Dinge, bewusst getrennt:

1. **Benachrichtigungen** landen in einer Inbox. Der Orb holt sie beim Aufwachen
   ab. Nur *explizite* Ereignisse – keine Spiegelung beliebiger Chat-Antworten.
2. **Gespraeche** laufen als echte Hermes-Runde in **deinem gewaehlten Profil**:
   mit dessen Modell, Provider, Persoenlichkeit, Erinnerungen und Werkzeugen.
3. **Erreichbarkeit** entscheidest **du**. Die Bridge bindet nur an
   `127.0.0.1` und vertraut einem von dir geprueften HTTPS-Weg.

## Was die Bridge nicht macht

Damit du nicht mehr erwartest, als drin ist:

- Kein Chat-Frontend, kein Ersatz fuer Telegram.
- Keine Sprachbefehle an den Orb, waehrend dessen Display aus ist.
- Kein Nachladen von Modellen, keine Auswahl bezahlter Anbieter.
- **Keine Sandbox.** Ein Profilname ist ein Routing-Label, keine
  Betriebssystemgrenze. Fuer Mehrbenutzerbetrieb: eigener Dienstbenutzer.
- Werkzeug-Nebenwirkungen werden **nicht** zurueckgenommen, wenn eine Runde ins
  Zeitlimit laeuft oder abgebrochen wird.

---

## Voraussetzungen

| Was | Details |
|---|---|
| Hermes Agent | installiert, Profil mit Modell und Provider konfiguriert |
| Python | 3.10 oder neuer |
| Privates Hermes-Home | eigenes Verzeichnis (`0700`), **nicht** dein Hauptprofil |
| Ein HTTPS-Weg | siehe [Erreichbarkeit](#erreichbarkeit-deine-entscheidung) |
| Optional: Spracherkennung | lokales Whisper-Modell oder Kommando-Adapter |
| Optional: Sprachausgabe | lokaler WAV-Endpunkt oder Kommando-Adapter |

## Schnellstart

```bash
git clone https://github.com/<dein-account>/si-m-bridge.git
cd si-m-bridge

python -m venv .venv && . .venv/bin/activate
pip install -e .

# 1. Weg nach draussen planen (schreibt nur Konfigdateien zum Pruefen)
python cli.py setup --exposure tailscale \
  --hermes-home /srv/hermes-orb --profile orb

# 2. Konfiguration als Dienst vorbereiten
export SIM_CONNECTOR_HERMES_HOME=/srv/hermes-orb
export SIM_CONNECTOR_DB=/srv/hermes-orb/sim-inbox.sqlite
export SIM_HERMES_PROFILE=orb
python cli.py --db "$SIM_CONNECTOR_DB" doctor

# 3. Dienst starten (nur Loopback)
python cli.py --db "$SIM_CONNECTOR_DB" serve
```

Koppeln (Schritt 4) erst, wenn der HTTPS-Weg wirklich antwortet:

```bash
python cli.py --db "$SIM_CONNECTOR_DB" pair \
  --owner owner-a --profile orb --device orb-a \
  --base-url https://orb-host.tail1234.ts.net
```

Der `pair`-Befehl gibt eine `sim://pair?...`-URI aus. Die gehoert in einen
QR-Code – **nicht** in Logs, Screenshots oder Chats.

---

## Erreichbarkeit: deine Entscheidung

Der Orb spricht **ausschliesslich HTTPS mit System-Zertifizierungsstellen**.
Ein einfaches `http://192.168.x.x:8766` ist deshalb keine Option – auch wenn es
bequem waere. Die Bridge lehnt solche URLs ab.

`python cli.py setup` fragt dich, welchen Weg du willst:

| Modus | Was du brauchst | Port nach aussen |
|---|---|---|
| `tailscale` | Tailscale auf dem Host, MagicDNS aktiv | nein |
| `caddy` | eigene Domain mit DNS auf den Host | 80 + 443 |
| `nginx` | nginx + Zertifikat (z. B. certbot) | 80 + 443 |
| `cloudflare` | Cloudflare-Account + Domain | nein |
| `existing` | du hast schon einen HTTPS-Proxy | – |
| `loopback` | nichts – **nur zum Testen** | nein |

Der Wizard **aendert nichts am System**. Er prueft, was vorhanden ist, sagt
ehrlich was fehlt und schreibt die Proxy-Konfiguration zum Reviewen nach
`./generated/`. Anwenden tust du sie selbst.

Ausfuehrlich: [docs/ERREICHBARKEIT.md](docs/ERREICHBARKEIT.md)

### Warum Tailscale der bequemste Weg ist

Kein Port im Router, kein DNS-Eintrag, kein Zertifikatsablauf:

```bash
tailscale cert orb-host.tail1234.ts.net
tailscale serve --bg --https=443 http://127.0.0.1:8766
tailscale serve status
```

Der Orb muss dafuer im selben Tailnet erreichbar sein (z. B. ueber einen
Netzknoten im Heimnetz).

---

## Den Agenten anbinden

Die Bridge laeuft **nicht** im Hauptprofil. Sie braucht ein eigenes Home:

```bash
export HERMES_HOME=/srv/hermes-orb     # vor dem Hermes-Import setzen
```

In diesem Home:

```yaml
# config.yaml
model:
  provider: <dein-provider>
  default: <dein-modell>

plugins:
  enabled:
    - si-m-bridge

platform_toolsets:
  cli:
    - si_m_bridge        # nur wenn der Agent benachrichtigen darf
```

Plugin installieren und pruefen:

```bash
mkdir -p "$HERMES_HOME/plugins/si-m-bridge"
cp plugin.yaml __init__.py connector.py audio_bridge.py \
   "$HERMES_HOME/plugins/si-m-bridge/"

HERMES_HOME=$HERMES_HOME hermes plugins enable si-m-bridge
HERMES_HOME=$HERMES_HOME hermes plugins doctor --ci "$HERMES_HOME/plugins/si-m-bridge"
```

Damit der Agent benachrichtigen darf, muessen diese Variablen gesetzt sein – die
Bridge weigert sich sonst:

```bash
SIM_CONNECTOR_OWNER=owner-a
SIM_CONNECTOR_PROFILE=orb
SIM_CONNECTOR_DB=$HERMES_HOME/sim-inbox.sqlite
```

Der Agent ruft dann `sim_notify` mit `event_id`, `kind` (`task_done` oder
`question`), `title` und `body` auf. Gleiche `event_id` = kein Duplikat.

---

## Sprache

Ohne Sprache funktioniert Text. Fuer Sprachrunden brauchst du Adapter:

```bash
# Spracherkennung: lokales Whisper-Modell ...
export SIM_STT_MODEL_PATH=/pfad/zu/faster-whisper-modell
# ... oder eigenes Kommando (PCM auf stdin, Text auf stdout)
export SIM_STT_COMMAND='["mein-stt", "--raw"]'

# Sprachausgabe: lokaler WAV-Endpunkt ...
export SIM_TTS_URL=http://127.0.0.1:8880
# ... oder eigenes Kommando (Text auf stdin, WAV auf stdout)
export SIM_TTS_COMMAND='["mein-tts"]'
```

Fehlt ein Adapter, antwortet die Bridge mit **503** statt mit Stille.

---

## Sicherheit

- Bindet standardmaessig nur an `127.0.0.1`; ein anderer Host wird verweigert.
- Geraete bekommen 32-Byte-Token; gespeichert wird nur der SHA-256-Hash.
- Kopplungscodes sind einmalig, 120 Sekunden gueltig, an Profil **und** Geraet
  gebunden.
- Jedes Geraet haengt an genau einem Owner/Profil-Paar – festgelegt vom
  Betreiber, nie vom Geraet.
- `binding`-Kennung pro Kopplung: neu gekoppelt heisst neuer Stand, alte Cursor
  werden verworfen.
- Inbox-Pfad muss der kanonische Pfad im Profil-Home sein; Symlinks und
  abweichende Pfade werden abgewiesen.
- Agentenrunden laufen in eigenen Prozessen: hoechstens zwei gleichzeitig,
  hartes Limit 90 Sekunden, bei Abbruch wird die Prozessgruppe beendet.
- Keine Zugangsdaten in Logs, keine QR-Codes in Ausgaben.

## Diagnose

```bash
python cli.py --db "$SIM_CONNECTOR_DB" doctor      # Konfiguration vollstaendig?
python -m unittest discover -s tests -q           # Selbsttest
tailscale serve status                            # Weg nach draussen
curl -sS -o /dev/null -w '%{http_code}\n' https://<host>/v1/inbox/meta
```

`401` = erreichbar, Token fehlt oder falsch. `502/504` = Proxy erreicht die
Bridge nicht. `503` = meistens Audio-Adapter fehlt oder Agentenrunde gescheitert.

### Optionale Kreuztests gegen Firmware und App

Zwei Tests vergleichen die Serverantworten mit dem echten C-Decoder der Firmware
und mit dem QR-Fixture der Android-App. Diese Projekte liegen in der
Entwicklungsumgebung **neben** diesem Repo. Ein frischer Klon hat sie nicht –
dann werden die Tests sauber **uebersprungen**, nicht rot. Wer sie mitlaufen
lassen will:

```bash
export SIM_FIRMWARE_PROJECT=/pfad/zu/notification-firmware/project
export SIM_APP_PROJECT=/pfad/zu/si-m-connector/project
python -m unittest discover -s tests -v
```

---

## Betriebsdetails

- **Profilbindung:** Jede Runde startet einen installierten Hermes `AIAgent` in
  einem eigenen Prozess unter dem festgelegten privaten `HERMES_HOME`, mit dessen
  Modell-/Provider-Aufloesung (inklusive Hermes-OAuth), `SOUL`, konfigurierten
  Erinnerungen, Skills und CLI-Toolsets. Es gibt keine `SIM_HERMES_*`-Overrides
  fuer Endpunkt oder Schluessel: Provider-Konfiguration bleibt im Profil.
- **Grenzen der Arbeit:** zwei Worker plaetze, 90 Sekunden Deadline je Runde,
  Belegung fuehrt zu `busy` statt zu Warteschlange. Bei Timeout oder Abbruch wird
  die Prozessgruppe beendet und der Worker eingesammelt, bevor der Platz frei
  wird.
- **Kompatibilitaet:** Die Bridge nutzt private Hermes-Integrations-APIs. Ein
  Fail-Closed-Check prueft deren Signaturen beim Start; bei Abweichung startet
  sie nicht, statt still falsch zu laufen. Getestet gegen die aktuell installierte
  Hermes-Version, nicht gegen einen Versionsbereich.
- **Inbox-Pfad:** CLI und Plugin muessen denselben kanonischen Pfad verwenden,
  sonst brechen `pair` und `serve` vor dem ersten Zugriff ab.
- **Benachrichtigungsumfang:** nur `task_done` und `question`; proaktive
  Nachrichten sind aus. Akustik bei ausgeschaltetem Display ist Sache der
  Firmware (Ruhezeit 22:00–07:00, ein leiser Ton, Display bleibt aus).

## Stand und Grenzen

Ehrlich, damit niemand mehr erwartet als da ist:

- **Getestet:** 37 Selbsttests, darunter eine echte Hermes-Runde in einem leeren
  Test-Home gegen einen synthetischen Test-Provider, Abbruch- und
  Zeitlimitverhalten echter Worker-Prozesse, Kopplungs- und Inbox-Vertraege
  gegen Firmware- und App-Seite.
- **Nicht getestet:** echtes Modell mit echten Zugangsdaten, physische
  Audiowiedergabe am Orb, Live-OAuth, ein produktiver Reverse Proxy.
- Die Firmware-Seite (Benachrichtigungen, Aufwachen) ist ein separater Kandidat
  und wird hier nicht ausgeliefert.

## Lizenz

Apache-2.0 – siehe [LICENSE](LICENSE).
