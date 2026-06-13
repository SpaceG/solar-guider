# Simple Solar Guider

**Prototyp** zur Sonnen-Nachführung (Auto-Guiding) für die **ZWO AM3** Montierung.

Simple Solar Guider liest das Bild einer Solar-Kamera ein, erkennt die Sonnenscheibe,
zeigt ein Live-Overlay an und kann – **nach manueller Erprobung** – die ZWO AM3 über
eine serielle COM-Schnittstelle mit LX200-ähnlichen Befehlen automatisch nachführen.

> Hinweis: Dies ist ein **Prototyp** für Windows. Die Entwicklung erfolgt
> plattformübergreifend (u. a. macOS), der produktive Betrieb ist jedoch für
> **Windows** vorgesehen.

---

## Inhaltsverzeichnis

1. [Überblick](#überblick)
2. [Projekt- und Modulstruktur](#projekt--und-modulstruktur)
3. [Installation](#installation)
4. [Hardware](#hardware)
5. [SharpCap-Ordnerquelle (optional)](#sharpcap-ordnerquelle-optional)
6. [Bedienung (Schritt für Schritt)](#bedienung-schritt-für-schritt)
7. [Sicherheit](#sicherheit)
8. [Wichtiger Hinweis zur AM3-Steuerung](#wichtiger-hinweis-zur-am3-steuerung)
9. [Erweiterbarkeit: ASCOM/Alpaca-Schnittstelle](#erweiterbarkeit-ascomalpaca-schnittstelle)

---

## Überblick

Die Anwendung besteht aus folgenden Funktionsblöcken:

- **Bildquelle**: Live-Kamera (OpenCV) **oder** ein Ordner, in den z. B. SharpCap
  laufend Bilder speichert.
- **Sonnenerkennung**: Schwellwert-basierte Erkennung der Sonnenscheibe, Berechnung
  des Versatzes (`dx`, `dy`) zum Bildmittelpunkt.
- **Live-Overlay**: Fadenkreuz, erkannter Sonnenmittelpunkt, Kreis um die Sonne und
  Statustexte.
- **Montierungssteuerung**: serielle LX200-ähnliche Befehle (Slew, Stopp,
  selbst-terminierende Guide-Pulse).
- **GUI** (PyQt6): Live-Bild links, Bedien- und Statuspanel rechts, manuelle
  Richtungstasten, Kalibrierung, Auto-Guide-Schalter, Not-Aus und ein Log-Fenster.

---

## Projekt- und Modulstruktur

Alle Module liegen im Ordner `solar_guider/` und importieren sich gegenseitig mit
**einfachen Modulnamen** (z. B. `from config import Config`). Das funktioniert, weil
`main.py` **aus dem Ordner `solar_guider/` heraus** gestartet wird.

```
solar_guider/
├── main.py              # Einstiegspunkt: startet die Qt-Anwendung
├── gui.py               # PyQt6-Oberfläche (MainWindow), Capture-/Guiding-Loop
├── config.py            # Config-Dataclass + JSON laden/speichern (config.json)
├── camera.py            # Bildquellen: OpenCVCamera, FolderImageSource, create_source
├── image_processing.py  # detect_sun, draw_overlay, SunDetection
├── mount_control.py     # SerialMount, MountInterface, list_serial_ports
├── requirements.txt     # Python-Abhängigkeiten
├── config.json          # wird beim Speichern der Einstellungen automatisch angelegt
└── README.md            # diese Datei
```

| Datei | Aufgabe |
|-------|---------|
| `main.py` | Initialisiert `QApplication`, zeigt `MainWindow`, startet die Event-Loop. |
| `gui.py` | Gesamte Bedienoberfläche, Live-Schleife (~15 fps), Auto-Guiding-Logik, Logging-Anzeige. |
| `config.py` | `Config`-Datenklasse mit allen Einstellungen; `load_config()` / `save_config()` nach `config.json`. |
| `camera.py` | Abstrakte Bildquelle `ImageSource`; Implementierungen für Kamera und Ordner; Fabrikfunktion `create_source()`. |
| `image_processing.py` | Erkennung der Sonnenscheibe und Zeichnen des Overlays. |
| `mount_control.py` | Serielle Montierungssteuerung; abstrakte Basisklasse als Erweiterungspunkt. |

---

## Installation

1. **Python installieren** (Version **3.10 oder neuer**).
   - Windows: Installer von [python.org](https://www.python.org/downloads/) herunterladen.
     Beim Setup unbedingt **„Add Python to PATH“** anhaken.
   - Installation prüfen (in der Eingabeaufforderung / PowerShell):
     ```bash
     python --version
     ```

2. **Abhängigkeiten installieren.** Im Ordner `solar_guider/`:
   ```bash
   pip install -r requirements.txt
   ```
   Dadurch werden installiert:
   - `opencv-python` (Bildverarbeitung)
   - `numpy` (Array-/Pixeloperationen)
   - `pyserial` (serielle COM-Kommunikation)
   - `PyQt6` (grafische Oberfläche)

> Optional (empfohlen): vorher eine virtuelle Umgebung anlegen
> (`python -m venv .venv` und aktivieren), damit die Abhängigkeiten isoliert sind.

---

## Hardware

### ZWO AM3 per USB verbinden

1. Die **ZWO AM3** über das **USB-Kabel** mit dem Windows-Rechner verbinden und die
   Montierung einschalten.
2. Windows installiert in der Regel automatisch einen virtuellen seriellen Treiber
   (USB-zu-Seriell). Bei Bedarf den passenden Treiber von ZWO nachinstallieren.

### COM-Port im Geräte-Manager finden

1. **Geräte-Manager** öffnen (Rechtsklick auf Start → *Geräte-Manager*, oder
   `devmgmt.msc` ausführen).
2. Abschnitt **„Anschlüsse (COM & LPT)“** aufklappen.
3. Den Eintrag der AM3 / des USB-Seriell-Adapters suchen, z. B. **`COM3`**.
   - Tipp: USB-Kabel ein-/ausstecken und beobachten, welcher `COMx`-Eintrag
     erscheint bzw. verschwindet.
4. Diesen Port (z. B. `COM3`) später in der App im COM-Port-Auswahlfeld einstellen.

> Die Baudrate ist standardmäßig **9600** und kann in der Oberfläche angepasst werden.

### Solar-Kamera

- Entweder direkt als **Live-Kamera** (OpenCV-Kameraindex, meist `0`)
- oder indirekt über einen **SharpCap-Ordner** (siehe nächster Abschnitt).

---

## SharpCap-Ordnerquelle (optional)

Wenn die Kamera nicht direkt von OpenCV gelesen werden kann (oder SharpCap bereits
läuft), kann die App stattdessen den **neuesten Bild-Datei** in einem Ordner
verwenden:

1. SharpCap so konfigurieren, dass es Einzelbilder **fortlaufend in einen Ordner
   speichert** (z. B. als PNG/JPG/BMP/TIF).
2. In der App als Bildquelle **„Folder“** wählen und über **„Browse SharpCap
   folder“** denselben Ordner auswählen.
3. Die App lädt automatisch immer die **neueste Datei** (nach Änderungszeit) aus
   diesem Ordner.

Unterstützte Bildformate für die Ordnerquelle: `.png`, `.jpg`, `.jpeg`, `.bmp`,
`.tif`, `.tiff`. (FITS wird in diesem Prototyp noch **nicht** unterstützt.)

---

## Bedienung (Schritt für Schritt)

Die App wird **aus dem Ordner `solar_guider/`** gestartet:

```bash
python main.py
```

Danach in dieser Reihenfolge vorgehen:

1. **App starten.** Das Hauptfenster zeigt links das Live-Bild, rechts das
   Bedienpanel.

2. **Bildquelle wählen.**
   - Für eine direkt angeschlossene Kamera: **„Camera“** wählen und den
     Kameraindex einstellen (meist `0`).
   - Für SharpCap: **„Folder“** wählen und den Speicherordner über **„Browse
     SharpCap folder“** auswählen.
   - Mit **„Start/Stop Live“** die Live-Anzeige einschalten.

3. **Sonne erkennen.**
   - Im Bild erscheinen Fadenkreuz (Bildmitte) und – bei Erkennung – ein Punkt im
     Sonnenmittelpunkt sowie ein Kreis um die Sonne.
   - Die Statusanzeigen zeigen `dx`, `dy`, `radius` und den Status (z. B. **„OK“**,
     **„No sun“**, **„Too small“**).
   - Falls nötig: **`threshold`** (Helligkeitsschwelle) und **`min_radius`**
     (Mindestradius) anpassen, bis die Sonne stabil als **„OK“** erkannt wird.

4. **Mount verbinden.**
   - Den richtigen **COM-Port** auswählen (ggf. **„Refresh“** drücken) und die
     **Baudrate** prüfen (Standard 9600).
   - **„Connect“** drücken. Die Mount-Statusanzeige sollte „verbunden“ zeigen.

5. **Zuerst MANUELL testen.**
   - Mit den Richtungstasten **North / South / East / West** kurze Test-Pulse
     auslösen (Pulsdauer = `manual_pulse_ms`).
   - Im Live-Bild prüfen, ob sich die Sonne in die **erwartete** Richtung bewegt.
   - Stimmt die Richtung nicht, **`invert_ra`** bzw. **`invert_dec`** umstellen.
   - **„Stop“** stoppt jede Bewegung sofort.

6. **Optional kalibrieren.**
   - **„Calibrate“** löst definierte Pulse aus und misst den Pixelversatz des
     Sonnenmittelpunkts, um `px_per_ms_ra` / `px_per_ms_dec` zu bestimmen.
   - Ohne Kalibrierung verwendet das Auto-Guiding einen **sicheren kurzen Puls**
     als Rückfallwert.

7. **Erst DANACH Auto Guide einschalten.**
   - Wenn die manuelle Steuerung korrekt funktioniert und die Sonne stabil erkannt
     wird, die Checkbox **„Auto Guide“** aktivieren (standardmäßig **AUS**).
   - Die App sendet nun in Intervallen (`correction_interval`) Korrekturpulse,
     solange `dx`/`dy` außerhalb des Totbands (`deadband_px`) liegen.

8. **Einstellungen speichern.**
   - Über **„Save settings“** werden alle Werte in `config.json` gesichert und beim
     nächsten Start automatisch geladen.

---

## Sicherheit

Die Anwendung ist so ausgelegt, dass **keine unkontrollierte Bewegung** entsteht.
Bitte folgende Punkte beachten:

- **Auto Guide ist standardmäßig AUS.** Es muss bewusst manuell aktiviert werden.
- **Keine Bewegung ohne erkannte Sonne.** Es werden **niemals** Befehle gesendet,
  wenn die Sonne nicht erkannt wurde (`detection.found == False`).
- **Bewegung nur unter drei Bedingungen gleichzeitig:**
  1. Auto Guide ist eingeschaltet,
  2. die Sonne ist erkannt **und**
  3. die Montierung ist verbunden.
- **Nur kurze Pulse.** Guide-Pulse sind selbst-terminierend und werden hart auf
  **maximal 1000 ms** begrenzt; Werte ≤ 0 werden ignoriert. Zusätzlich begrenzt
  `max_pulse_ms` die Korrekturdauer.
- **Erst manuell testen.** Vor dem Auto-Guiding immer die manuellen Richtungstasten
  nutzen und die Bewegungsrichtung verifizieren.
- **Not-Aus immer sichtbar.** Der große **EMERGENCY STOP**-Knopf stoppt **alle**
  Bewegungen (`:Q#`) und schaltet Auto Guide sofort wieder aus.
- **Logs mitlesen.** Jeder gesendete Befehl wird im Log-Fenster protokolliert.
  Bei Auffälligkeiten sofort Not-Aus betätigen.

> Achten Sie außerdem auf den mechanischen Bewegungsbereich der Montierung und auf
> Kabel/Hindernisse, um Kollisionen zu vermeiden.

---

## Wichtiger Hinweis zur AM3-Steuerung

> **Direkte Steuerung der AM3 ohne ASCOM benötigt das korrekte ZWO-Protokoll oder
> ASCOM/Alpaca.**

Dieser Prototyp sendet **LX200-ähnliche** Befehle über die serielle Schnittstelle.
Die ZWO AM3 akzeptiert solche rohen LX200-Befehle über Serial **möglicherweise
nicht** direkt. Für einen zuverlässigen Betrieb ist in der Regel entweder das
**korrekte ZWO-Protokoll** oder eine Anbindung über **ASCOM/Alpaca** erforderlich.

---

## Erweiterbarkeit: ASCOM/Alpaca-Schnittstelle

Die Montierungssteuerung ist bewusst über eine abstrakte Basisklasse entworfen, damit
zukünftige Backends (z. B. ZWO-Protokoll, ASCOM oder Alpaca) **ohne Änderungen am
restlichen Code** ergänzt werden können.

- Die abstrakte Basisklasse **`MountInterface`** (in `mount_control.py`) definiert die
  einheitliche Schnittstelle:
  `connect()`, `disconnect()`, `is_connected()`, `move(direction)`, `stop()`,
  `pulse(direction, ms)`.
- Die aktuelle Implementierung **`SerialMount`** verwendet LX200-ähnliche serielle
  Befehle und ist die **dokumentierte Nahtstelle** für die Anbindung des
  ZWO-Protokolls bzw. von ASCOM/Alpaca.
- Ein neues Backend wird einfach als weitere Unterklasse von `MountInterface`
  implementiert (z. B. `class AscomMount(MountInterface): ...`). Die GUI nutzt nur die
  Schnittstellenmethoden und bleibt dadurch unverändert.

Die Richtungs-Strings sind im gesamten Projekt einheitlich: **`"N"`, `"S"`, `"E"`,
`"W"`**.

---

*Simple Solar Guider — Prototyp. Verwendung auf eigene Verantwortung. Vor dem
Auto-Guiding immer manuell testen und den Not-Aus bereithalten.*
