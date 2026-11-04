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

## Installation und Start (Quell-Export)

Die Bridge bietet zwei Runtime-Modi: `gateway` verbindet einen bestehenden lokalen,
authentifizierten Hermes-Gateway; `embedded` benutzt interne Hermes-Python-APIs und
benötigt deshalb denselben Python-Interpreter wie Hermes. Wähle den Modus bewusst.
Ein frischer Python-Interpreter ohne Hermes kann nur den Gateway-Modus ausführen.
Keine Installation verändert automatisch dein Hermes-Profil oder deinen HTTPS-Proxy.

```bash
git clone https://github.com/jokaje/si-m-bridge.git
cd si-m-bridge
# In einem eigens gewählten Python-Environment (nicht blind im Live-Hermes-venv):
python -m pip install .
si-m-bridge --help
mkdir -p ~/si-m-bridge-state && chmod 700 ~/si-m-bridge-state
export SIM_CONNECTOR_HERMES_HOME=~/si-m-bridge-state
export SIM_CONNECTOR_DB=$SIM_CONNECTOR_HERMES_HOME/sim-inbox.sqlite
export SIM_CONNECTOR_OWNER=owner-a SIM_CONNECTOR_PROFILE=default
export SIM_HERMES_PROFILE=default SIM_AGENT_MODE=gateway
export SIM_GATEWAY_URL=http://127.0.0.1:8642
export SIM_GATEWAY_KEY_FILE=$HOME/.config/si-m-bridge/gateway-key
# Die Schlüsseldatei muss den vorhandenen Gateway-API-Schlüssel enthalten und 0600 sein.
si-m-bridge --db "$SIM_CONNECTOR_DB" doctor
si-m-bridge --db "$SIM_CONNECTOR_DB" serve
```

Der Gateway muss bereits laufen und die API authentifizieren; `doctor` prüft die
Verbindung. Die Gateway-URL ist ein **lokaler Beispielwert**, kein voreingestellter
privater Agent. Lege die Schlüsseldatei außerhalb des Repositories an; nie einen
Schlüssel in Shell-History, QR oder Logs ausgeben. Im Gateway-Modus bleibt der
Bridge-State separat; Orb-Turns nutzen eigene Sessions im gewählten Agentenprofil,
nicht den Telegram-Verlauf. Benachrichtigungen aus Hermes brauchen zusätzlich eine
explizite Plugin-Installation mit identischem DB-/Owner-/Profil-Binding.

Im `embedded`-Modus installiere die Bridge in einer kompatiblen Hermes-Python-Umgebung,
setze `SIM_AGENT_MODE=embedded` und `SIM_CONNECTOR_HERMES_HOME` auf ein privates
Hermes-Home mit konfigurertem Modell/Provider. `SIM_HERMES_PROFILE` bezeichnet das
gewählte Profil. Der Kompatibilitätscheck für interne APIs verweigert unbekannte
Hermes-Versionen; das ist keine Garantie für alle Hermes-Installationen.

Für HTTPS wähle `si-m-bridge setup --exposure nginx --domain bridge.example.com
--no-write` (oder `caddy`, `cloudflare`, `existing`) und prüfe die generierten
Konfigurationen vor Anwendung. Der Server selbst bindet ausschließlich Loopback.
Der Orb braucht eine aus seinem WLAN erreichbare HTTPS-Adresse mit gültiger
System-CA; Tailscale Serve allein genügt nicht. Erst nach Prüfung von DNS, TLS,
Proxy und unauthentifiziertem `401` koppeln:

```bash
si-m-bridge --db "$SIM_CONNECTOR_DB" pair \
  --owner owner-a --profile default --device orb-a \
  --base-url https://bridge.example.com
```

Die ausgegebene kurzlebige `sim://pair`-URI nur **lokal** als QR darstellen;
nicht in Berichten, Screenshots oder fremden QR-Diensten veröffentlichen.

---

## Erreichbarkeit: deine Entscheidung

Der Orb spricht **ausschliesslich HTTPS mit System-Zertifizierungsstellen**.
Ein einfaches `http://192.168.x.x:8766` ist deshalb keine Option – auch wenn es
bequem waere. Die Bridge lehnt solche URLs ab.

`python cli.py setup` fragt dich, welchen Weg du willst. Voraussetzungen und
Geraete-Erreichbarkeit muessen ausserhalb des Wizards wirklich geprueft werden:

| Modus | Was du brauchst | Port nach aussen |
|---|---|---|
| `tailscale` | Nur Tailnet-Clients; fuer WLAN-Orb ohne Tailscale **nicht geeignet** | nein |
| `caddy` | eigene Domain mit DNS auf den Host | 80 + 443 |
| `nginx` | nginx + Zertifikat (z. B. certbot) | 80 + 443 |
| `cloudflare` | Cloudflare-Account + Domain | nein |
| `existing` | du hast schon einen HTTPS-Proxy | – |
| `loopback` | nichts – **nur zum Testen** | nein |

Der Wizard **aendert nichts am System**. Er prueft, was vorhanden ist, sagt
ehrlich was fehlt und schreibt die Proxy-Konfiguration zum Reviewen nach
`./generated/`. Anwenden tust du sie selbst.

Ausfuehrlich: [docs/ERREICHBARKEIT.md](docs/ERREICHBARKEIT.md)

### Tailscale Serve: kein direkter Weg fuer den WLAN-Orb

Serve exponiert nur im Tailnet, nicht im normalen WLAN:

```bash
tailscale cert orb-host.tail1234.ts.net
tailscale serve --bg --https=443 http://127.0.0.1:8766
tailscale serve status
```

Der Orb hat keinen Tailscale-Client. Gleiches WLAN oder ein Tailscale-Subnet-Router
auf einem anderen Geraet machen ihn nicht zum Tailnet-Client. Fuer Pairing eine
vom Orb erreichbare HTTPS-Domain mit vertrauenswuerdigem Zertifikat waehlen.

---

## Benachrichtigungs-Plugin

`sim_notify` schreibt nur explizite Ereignisse (`task_done`, `question`) in die
Inbox. Für den eingebetteten Agenten oder den bestehenden Gateway-Agenten ist
eine **separate** Plugin-Aktivierung im richtigen Hermes-Profil erforderlich;
der Bridge-Server allein aktiviert weder Plugin noch Toolset. Verwende denselben
kanonischen `SIM_CONNECTOR_DB`-Pfad und `SIM_CONNECTOR_OWNER` /
`SIM_CONNECTOR_PROFILE` wie beim Server. Siehe [Protokoll](docs/PROTOCOL.md).

---

## Sprache

Ohne Sprache funktioniert Text. Fuer Sprachrunden brauchst du Adapter:

```bash
# Spracherkennung: lokales Whisper-Modell ...
export SIM_STT_MODEL_PATH=/pfad/zu/faster-whisper-modell
# ... mit Sprachhinweis (deutlich treffsicherer als raten lassen)
export SIM_STT_LANGUAGE=de
# ... oder eigenes Kommando (PCM auf stdin, Text auf stdout)
export SIM_STT_COMMAND='["mein-stt", "--raw"]'

# Sprachausgabe: lokaler WAV-Endpunkt ...
export SIM_TTS_URL=http://127.0.0.1:8880
# ... oder eigenes Kommando (Text auf stdin, WAV auf stdout)
export SIM_TTS_COMMAND='["mein-tts"]'
```

Sprache ist optional; die UI muss Text ohne STT erlauben. Lokales Whisper wird erst
im isolierten Spawn-Worker aus dem Modellpfad geladen (pro Anfrage, mit zusaetzlicher
Startlatenz). Synchrone Adapter laufen mit zwei Plaetzen und Deadline in killbaren
Spawn-Prozessen; `cloudpickle` ist dafuer eine Pflichtabhaengigkeit. Das begrenzt
haengende Erkennung, garantiert aber keine Ruecknahme bereits erfolgter Nebenwirkungen.

Fehlt ein Adapter, antwortet die Bridge mit **503** statt mit Stille.

**Modellwahl ist nicht egal.** Gemessen auf CPU mit einer deutschen Testfrage:
`tiny` und `base` verhoeren kurze Sätze regelmäßig (aus „Test der Brücke" wurde
„10時でブーク"), `small` erkennt sie korrekt. `SIM_STT_LANGUAGE` hilft auch dem
kleinen Modell, ersetzt aber kein ausreichend großes. Nimm `small` oder größer.

Der Sprachhinweis sollte aus der Sprachwahl kommen, die der Nutzer in App und Orb
gesetzt hat — nicht geraten werden.

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

## Betriebsdetails und Grenzen

- Gateway-Turns nutzen den bestehenden authentifizierten Gateway mit eigener
  Orb-Session; eingebettete Turns nutzen private Hermes-Python-APIs und einen
  fail-closed Signaturcheck. Beide haben maximal zwei Workerplätze und 90 Sekunden
  Deadline. Abbruch macht Tool-Nebenwirkungen nicht rückgängig.
- Sprache ist optional; echte STT/TTS-Adapter erfordern lokale Modelle oder Dienste.
  Synthetische Tests belegen weder hörbare Ausgabe am Gerät noch Qualität echter
  Sprachmodelle. Firmware und Android-App werden getrennt ausgeliefert.
- Die Suite im frischen Klon überspringt Kreuztests ohne Nachbarprojekte und
  externe Adapter; Testanzahl und Skips sind keine feste Produktzusage.
- Installation, Proxy-Aktivierung, Plugin-Bindung, Zertifikat und ein hörbarer
  Geräteturn müssen auf dem Zielsystem separat geprüft werden.

## Lizenz

Apache-2.0 – siehe [LICENSE](LICENSE).
