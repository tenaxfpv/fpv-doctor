"""
FPV Doctor - finestra grafica.

Avvia il motore collaudato (fpv_app.py) e lo mostra in una finestra con pulsanti.
Funziona solo sul tuo computer (indirizzo 127.0.0.1), senza internet.

Avvio:  python3 fpv_gui.py      (poi l'app propone di creare l'icona sulla Scrivania)
"""

import builtins
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)
import fpv_app as A  # noqa: E402  (il motore)
try:   # prove al banco (file separato: se manca o ha un problema, il resto dell'app funziona lo stesso)
    import fpv_banco as B  # noqa: E402
except Exception as _errore_banco:   # noqa: BLE001
    B = None

PORTA = 8765
INDIRIZZO = f"http://127.0.0.1:{PORTA}/"
ICONA = os.path.expanduser("~/Desktop/FPV Doctor.app")

blocco = threading.RLock()
STATO = {
    "sessione": 0,        # cambia a ogni nuova operazione: la pagina ricomincia l'elenco
    "titolo": "",
    "righe": [],
    "live": "",
    "richiesta": None,    # domanda in attesa di risposta
    "in_corso": None,
    "drone": None,
    "immagine": None,
    "dopo_risposta": 0,   # indice della prima riga stampata dopo l'ultima risposta
}
risposta = {"evento": threading.Event(), "valore": None}
lavoro = {"thread": None}


# ============================================================== uscita del motore verso la pagina

class Uscita:
    """Riceve quello che il motore stampa. Le righe con '\\r' (valori in tempo reale) vanno nel
    riquadro 'live' e non nell'elenco."""

    def __init__(self):
        self.buf = ""

    def write(self, testo):
        testo = (testo.replace("Ctrl + C", "il pulsante Interrompi")
                 .replace("leggi l'istruzione, premi Invio,", "leggi l'istruzione, premi Sono pronto,"))
        with blocco:
            self.buf += testo
            while "\n" in self.buf:
                riga, self.buf = self.buf.split("\n", 1)
                if "\r" in riga:
                    STATO["live"] = ""
                elif riga.strip():
                    STATO["righe"].append(riga.rstrip())
                    m = re.match(r"\s*Grafico salvato in:\s*(.+\.png)\s*$", riga)
                    if m:
                        STATO["immagine"] = m.group(1)
            if "\r" in self.buf:
                ultimo = self.buf.rsplit("\r", 1)[-1]
                STATO["live"] = ultimo.strip()
                self.buf = "\r" + ultimo
        return len(testo)

    def flush(self):
        pass


def ultima_istruzione():
    for r in reversed(STATO["righe"]):
        if r.lstrip().startswith("▶"):
            return re.sub(r"^\s*▶\s*(PASSO \d+:\s*)?", "", r).strip()
    return ""


def righe_nuove():
    """Solo le righe stampate dopo la risposta precedente: così una domanda non eredita le scelte di un'altra."""
    return STATO["righe"][STATO["dopo_risposta"]:]


def opzioni_recenti():
    """Le scelte stampate appena prima della domanda (righe '  1. ...' oppure 'Log 12: ...')."""
    opzioni = []
    for r in reversed(righe_nuove()[-40:]):
        m = re.match(r"^\s*(?:Log\s+)?(\d+)[.:]\s+(.*)$", r)
        if m and "[ ]" not in r:
            etichetta = m.group(2).strip()
            if r.lstrip().startswith("Log"):
                etichetta = f"Log {m.group(1)}: {etichetta}"
            opzioni.insert(0, {"valore": m.group(1), "etichetta": etichetta})
        elif opzioni:
            break
    voli = [o for o in opzioni if "(volo" in o["etichetta"]]
    if voli:   # nella scelta del log mostro solo i voli veri
        opzioni = voli
    return opzioni


RUMORE = ("Salvo una copia", "Copia salvata", "Decodifico", "Cerco il drone", "Allowed", "String length",
          "Default value")


def contesto_recente():
    """La spiegazione stampata dopo l'ultima istruzione ▶ (es. perché e di quanto di una correzione)."""
    righe = righe_nuove()
    for i in range(len(righe) - 1, max(len(righe) - 13, -1), -1):   # solo se l'istruzione è vicina alla domanda
        if righe[i].lstrip().startswith("▶"):
            utili = [r.strip() for r in righe[i:] if r.strip() and not r.strip().startswith(RUMORE)
                     and not re.match(r"^\s*(?:Log\s+)?\d+[.:]\s", r) and not r.lstrip().startswith("↳")]
            return utili[:8]
    return []


def diario(testo):
    """Diario delle operazioni: cosa è stato chiesto, cosa hai risposto, come è finita.
    Serve a Claude per capire cosa è successo senza doverti chiedere 'cosa vedi'."""
    try:
        percorso = os.path.join(BASE, "fpv_gui_diario.log")
        if os.path.exists(percorso) and os.path.getsize(percorso) > 400_000:
            os.replace(percorso, percorso + ".vecchio")
        with open(percorso, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%d/%m %H:%M:%S')}  {testo}\n")
    except OSError:
        pass


def input_grafico(domanda=""):
    """Sostituisce input(): mostra la domanda nella pagina e aspetta la risposta."""
    testo = domanda.strip()
    if "quando sei pronto" in testo:
        tipo, testo = "pronto", ultima_istruzione()
    elif "(s/n)" in testo:
        tipo, testo = "sino", testo.replace("(s/n):", "").replace("(s/n)", "").strip()
    elif "CANCELLA" in testo:
        tipo = "cancella"
    else:
        tipo = "testo"
    predefinito = ""
    m = re.search(r"\[Invio = ([^\],]+)", testo)
    if m:
        predefinito = m.group(1).strip()
    testo = re.sub(r"\s*\[Invio[^\]]*\]", "", testo).replace("(Invio per annullare)", "").strip()
    with blocco:
        risposta["evento"].clear()
        STATO["richiesta"] = {"tipo": tipo, "testo": testo.rstrip(": "), "predefinito": predefinito,
                              "opzioni": opzioni_recenti() if tipo == "testo" else [],
                              "contesto": [] if (tipo == "testo" and opzioni_recenti()) else
                                          contesto_recente() if tipo in ("testo", "sino") else [],
                              "id": time.time()}
    while not risposta["evento"].wait(0.2):   # attesa a scatti: così 'Interrompi' funziona
        pass
    diario(f"   domanda: {testo[:120]!r} opzioni={[o[0] if isinstance(o, (list, tuple)) else o for o in (STATO['richiesta'] or {}).get('opzioni', [])][:12]}")
    with blocco:
        valore = risposta["valore"] or ""
        diario(f"   risposta: {valore!r}")
        STATO["richiesta"] = None
        STATO["dopo_risposta"] = len(STATO["righe"])
        if tipo == "sino":
            STATO["righe"].append(f"   ↳ {'Sì' if valore == 's' else 'No'}")
        elif tipo == "testo" and valore:
            STATO["righe"].append(f"   ↳ {valore}")
    return valore


def apri_immagine(args, **kw):
    if args and args[0] == "open":
        with blocco:
            STATO["immagine"] = args[-1]


# ============================================================== operazioni

def riassunto_componenti(drone):
    if not drone or not drone.get("nome"):
        return None
    c = A.leggi_componenti(drone["nome"])
    radio = c.get("ricevitore", "")
    controllo = A.confronta_radio(radio, drone.get("protocollo"))
    righe = []
    kv = A.kv_motori(c)
    if c.get("motori") or kv:
        testo = c.get("motori", "")
        if kv and str(kv) not in testo:
            testo = f"{testo} {kv} KV".strip()
        righe.append(["Motori", testo])
    if c.get("frame"):
        righe.append(["Frame", c["frame"]])
    if c.get("eliche"):
        righe.append(["Eliche", c["eliche"]])
    if c.get("celle_batteria"):
        tipo = A.profilo_batteria(c)["nome"].split(" ")[0] if c.get("tipo_batteria") else ""
        righe.append(["Batteria", f"{c['celle_batteria']}S {tipo}".strip()])
    if radio or drone.get("protocollo"):
        righe.append(["Radio", radio or "da indicare"])
    if c.get("vtx"):
        righe.append(["Video", c["vtx"]])
    ril = A.leggi_rilevati(drone)
    if ril.get("porte"):
        righe.append(["Porte", " · ".join(ril["porte"])])
    nota = None
    if ril.get("diversi"):
        nota = ["att", "Da verificare: " + "; ".join(ril["diversi"])]
    elif controllo is True:
        nota = ["ok", f"La scheda usa {drone['protocollo']}, coerente con {radio}"]
    elif controllo is False:
        nota = ["att", f"La scheda usa {drone['protocollo']}, che non corrisponde a {radio}"]
    elif drone.get("protocollo") and not radio:
        nota = ["ver", f"La scheda usa {drone['protocollo']}: indica la radio nei componenti"]
    return {"righe": righe, "nota": nota, "vuoto": not righe}


def riconosci_drone():
    print("Cerco il drone collegato ...")
    if A.trova_porta(attesa=3):
        drone = A.riconosci()
        print(f"Collegato: {drone['nome']} ({drone['scheda']}, Betaflight {drone['versione']})")
        return drone
    print("Nessun drone collegato. Collega il cavo USB e premi Ricollega.")
    print("Per analizzare un volo già scaricato non serve il drone.")
    return None


OPERAZIONI = {
    "officina": ("Controllo in officina", A.menu_officina, True),
    "configurazione": ("Configurazione", A.menu_configurazione, True),
    "ricevente": ("Ricevente", A.menu_test_rx, True),
    "esc": ("ESC e batteria", A.menu_test_esc, True),
    "scarica": ("Scarica il blackbox", A.menu_scarica, True),
    "analisi": ("Analizza un volo", A.menu_analisi, False),
    "cancella": ("Cancella i log dal drone", A.menu_cancella, True),
    "modifica": ("Correggi e migliora", A.menu_correggi, False),
    "riferimenti": ("Riferimenti Betaflight", A.menu_riferimenti, False),
    "ricollega": ("Ricollega il drone", None, False),
}
if B:
    OPERAZIONI.update({
        "motori": ("Motori: ordine e verso", B.menu_motori, True),
        "assetto": ("Assetto della scheda", B.menu_assetto, True),
        "vibrazioni": ("Vibrazioni dei motori", B.menu_vibrazioni, True),
    })
CON_MOTORI = ("motori", "vibrazioni")


def esegui(chiave):
    titolo, funzione, serve_usb = OPERAZIONI[chiave]
    inizio = time.time()
    d = STATO.get("drone") or {}
    diario(f"INIZIO {chiave}  drone={d.get('nome')!r} scheda={d.get('scheda')!r} python={sys.version.split()[0]}")
    try:
        if chiave == "ricollega":
            STATO["drone"] = riconosci_drone()
            return
        drone = STATO["drone"]
        if serve_usb and (not drone or drone.get("scheda") == "non collegato"):
            print("Per questa operazione il drone deve essere collegato con il cavo USB.")
            print("Collegalo e premi Ricollega.")
            return
        if not drone:
            drone = A.scegli_drone_offline()
            if not drone:
                print("Nessun drone scelto.")
                return
        funzione(drone)
    except KeyboardInterrupt:
        diario("   interrotta con il pulsante Interrompi")
        print("Operazione interrotta. Nessuna modifica in sospeso.")
    except Exception as errore:   # noqa: BLE001
        testo = str(errore)
        if "Device not configured" in testo or "read failed" in testo or "write failed" in testo \
                or "could not open port" in testo:
            print("❌ IL DRONE SI È SCOLLEGATO DURANTE IL CONTROLLO")
            print("  • Il cavo USB non risponde più")
            print("    Cosa significa: Il collegamento si è interrotto a metà: cavo mosso o lento, oppure la "
                  "scheda si è riavviata (per esempio attaccando o staccando la batteria).")
            print("    Cosa fare: Controlla che il cavo sia ben inserito, premi Ricollega e ripeti il controllo. "
                  "Sul drone non è stato modificato nulla.")
        else:
            print(f"Qualcosa è andato storto: {errore}")
            print("Copia questo messaggio e mandalo a Claude.")
        import traceback
        with open(os.path.join(BASE, "fpv_gui_errori.log"), "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%d/%m/%Y %H:%M:%S')} {chiave}\n")
            traceback.print_exc(file=f)
    finally:
        if chiave in CON_MOTORI and B:
            B.ferma_tutto()   # in ogni caso, i motori restano fermi
        with blocco:
            ultime = [r for r in STATO["righe"][-12:] if r.strip()]
        diario(f"FINE {chiave} dopo {time.time() - inizio:.0f} s. Ultime righe:")
        for r in ultime:
            diario(f"   | {r[:160]}")
        with blocco:
            STATO["in_corso"] = None
            STATO["live"] = ""
            STATO["richiesta"] = None


def avvia(chiave):
    with blocco:
        if STATO["in_corso"]:
            return False
        STATO["sessione"] += 1
        STATO["titolo"] = OPERAZIONI[chiave][0]
        STATO["righe"] = []
        STATO["dopo_risposta"] = 0
        STATO["live"] = ""
        STATO["immagine"] = None
        STATO["in_corso"] = chiave
    t = threading.Thread(target=esegui, args=(chiave,), daemon=True)
    lavoro["thread"] = t
    t.start()
    return True


def interrompi():
    t = lavoro["thread"]
    if t and t.is_alive():
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(t.ident), ctypes.py_object(KeyboardInterrupt))
        risposta["valore"] = ""
        risposta["evento"].set()


# ============================================================== icona sulla Scrivania

def crea_icona():
    cont = os.path.join(ICONA, "Contents")
    os.makedirs(os.path.join(cont, "MacOS"), exist_ok=True)
    os.makedirs(os.path.join(cont, "Resources"), exist_ok=True)
    eseguibile = os.path.join(cont, "MacOS", "fpv_doctor")
    with open(eseguibile, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n"
                f'cd "{BASE}"\n'
                f'PY="{sys.executable}"\n'
                f'APP="{os.path.join(BASE, "fpv_gui.py")}"\n'
                f'LOG="{os.path.join(BASE, "fpv_gui.log")}"\n'
                '# Su Apple Silicon parte sempre in modalità nativa (arm64), mai Intel/Rosetta\n'
                'if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then\n'
                '  nohup arch -arm64 "$PY" "$APP" >> "$LOG" 2>&1 &\n'
                'else\n'
                '  nohup "$PY" "$APP" >> "$LOG" 2>&1 &\n'
                'fi\n')
    os.chmod(eseguibile, 0o755)
    con_icona = disegna_icona(os.path.join(cont, "Resources"))
    with open(os.path.join(cont, "Info.plist"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '<key>CFBundleName</key><string>FPV Doctor</string>\n'
                '<key>CFBundleDisplayName</key><string>FPV Doctor</string>\n'
                '<key>CFBundleIdentifier</key><string>local.fpvdoctor</string>\n'
                '<key>CFBundleExecutable</key><string>fpv_doctor</string>\n'
                '<key>CFBundlePackageType</key><string>APPL</string>\n'
                '<key>CFBundleShortVersionString</key><string>0.1</string>\n'
                + ('<key>CFBundleIconFile</key><string>icona</string>\n' if con_icona else '') +
                '<key>LSUIElement</key><true/>\n'
                '<key>LSArchitecturePriority</key><array><string>arm64</string><string>x86_64</string></array>\n'
                '<key>LSRequiresNativeExecution</key><true/>\n'
                '</dict></plist>\n')
    subprocess.run(["touch", ICONA], check=False)
    return True


def disegna_icona(cartella_risorse):
    """Icona: drone stilizzato visto dall'alto. Se qualcosa manca, l'app resta senza icona personalizzata."""
    try:
        from PIL import Image, ImageDraw
        import tempfile
        set_icone = os.path.join(tempfile.mkdtemp(), "icona.iconset")
        os.makedirs(set_icone)
        grande = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
        d = ImageDraw.Draw(grande)
        d.rounded_rectangle((64, 64, 960, 960), radius=200, fill=(23, 49, 82, 255))
        c = 512
        for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            d.line((c, c, c + dx * 250, c + dy * 250), fill=(214, 224, 235, 255), width=46)
            d.ellipse((c + dx * 250 - 120, c + dy * 250 - 120, c + dx * 250 + 120, c + dy * 250 + 120),
                      outline=(214, 224, 235, 255), width=26)
        d.rounded_rectangle((c - 95, c - 130, c + 95, c + 130), radius=40, fill=(242, 169, 59, 255))
        for lato in (16, 32, 64, 128, 256, 512):
            grande.resize((lato, lato), Image.LANCZOS).save(os.path.join(set_icone, f"icon_{lato}x{lato}.png"))
            grande.resize((lato * 2, lato * 2), Image.LANCZOS).save(os.path.join(set_icone, f"icon_{lato}x{lato}@2x.png"))
        esito = subprocess.run(["iconutil", "-c", "icns", set_icone, "-o", os.path.join(cartella_risorse, "icona.icns")],
                               capture_output=True)
        return esito.returncode == 0
    except Exception:   # noqa: BLE001
        return False


# ============================================================== server locale

class Gestore(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, dati, codice=200):
        corpo = json.dumps(dati).encode("utf-8")
        self.send_response(codice)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            corpo = PAGINA.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        elif url.path == "/api/stato":
            da = int(parse_qs(url.query).get("da", ["0"])[0])
            sessione = int(parse_qs(url.query).get("sessione", ["-1"])[0])
            with blocco:
                if sessione != STATO["sessione"]:
                    da = 0
                self._json({
                    "sessione": STATO["sessione"], "titolo": STATO["titolo"],
                    "righe": STATO["righe"][da:], "totale": len(STATO["righe"]),
                    "live": STATO["live"], "richiesta": STATO["richiesta"],
                    "in_corso": STATO["in_corso"], "drone": STATO["drone"],
                    "immagine": bool(STATO["immagine"]),
                    "icona": os.path.exists(ICONA),
                    "componenti": riassunto_componenti(STATO["drone"]),
                })
        elif url.path == "/api/glossario":
            self._json(A.GLOSSARIO)
        elif url.path == "/api/operazioni":
            self._json(list(OPERAZIONI))
        elif url.path == "/api/ping":
            self._json({"ok": True})
        elif url.path == "/api/componenti":
            drone = STATO["drone"]
            valori = A.leggi_componenti(drone["nome"]) if drone else {}
            if valori and not valori.get("kv_motori") and A.kv_motori(valori):
                valori = dict(valori, kv_motori=str(A.kv_motori(valori)))   # KV ricavato dal testo dei motori
            self._json({"campi": [{"chiave": c[0], "etichetta": c[1], "aiuto": c[2], "scelte": c[3]}
                                  for c in A.CAMPI_COMPONENTI],
                        "valori": valori, "protocollo": (drone or {}).get("protocollo"),
                        "letti": A.leggi_rilevati(drone).get("letti", []) if drone else []})
        elif url.path == "/immagine":
            percorso = STATO["immagine"]
            if not percorso:
                self.send_error(404)
                return
            percorso = os.path.abspath(percorso)
            if not percorso.startswith(BASE) or not os.path.exists(percorso):
                self.send_error(404)
                return
            corpo = open(percorso, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        else:
            self.send_error(404)

    def do_POST(self):
        lunghezza = int(self.headers.get("Content-Length", "0") or 0)
        dati = json.loads(self.rfile.read(lunghezza) or b"{}")
        url = urlparse(self.path)
        if url.path == "/api/azione":
            chiave = dati.get("azione", "")
            self._json({"ok": chiave in OPERAZIONI and avvia(chiave)})
        elif url.path == "/api/risposta":
            risposta["valore"] = str(dati.get("valore", ""))
            risposta["evento"].set()
            self._json({"ok": True})
        elif url.path == "/api/interrompi":
            interrompi()
            self._json({"ok": True})
        elif url.path == "/api/icona":
            try:
                self._json({"ok": crea_icona()})
            except Exception as errore:   # noqa: BLE001
                self._json({"ok": False, "errore": str(errore)})
        elif url.path == "/api/componenti":
            drone = STATO["drone"]
            if not drone:
                self._json({"ok": False, "errore": "Nessun drone"})
            else:
                A.scrivi_componenti(drone["nome"], dati.get("valori", {}))
                self._json({"ok": True})
        elif url.path == "/api/esci":
            self._json({"ok": True})
            threading.Timer(0.3, lambda: os._exit(0)).start()
        else:
            self.send_error(404)


def gia_aperto():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORTA)) == 0


def apri_finestra():
    if os.environ.get("FPV_NESSUNA_FINESTRA"):
        return
    chrome = "/Applications/Google Chrome.app"
    if sys.platform == "darwin" and os.path.exists(chrome):
        subprocess.Popen(["open", "-na", chrome, "--args", f"--app={INDIRIZZO}", "--window-size=1200,820"])
    else:
        webbrowser.open(INDIRIZZO)


def sotto_rosetta():
    """True se il Mac è Apple Silicon ma questo Python gira in modalità Intel (Rosetta):
    in quel caso le librerie di calcolo installate per il Mac vero non si caricano."""
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def icona_da_aggiornare():
    eseguibile = os.path.join(ICONA, "Contents", "MacOS", "fpv_doctor")
    try:
        return os.path.exists(eseguibile) and "arch -arm64" not in open(eseguibile, encoding="utf-8").read()
    except OSError:
        return False


def main():
    # Su Apple Silicon l'app deve girare in modalità nativa (arm64), non Intel tramite Rosetta.
    if sotto_rosetta() and not os.environ.get("FPV_RIAVVIO_NATIVO"):
        os.environ["FPV_RIAVVIO_NATIVO"] = "1"
        os.execv("/usr/bin/arch", ["arch", "-arm64", sys.executable, os.path.abspath(__file__)])
    if icona_da_aggiornare():
        try:
            crea_icona()   # l'icona sulla Scrivania d'ora in poi avvia l'app in modalità nativa
        except OSError:
            pass
    if gia_aperto():
        apri_finestra()
        return
    builtins.input = input_grafico
    class SoloGrafico:   # il motore apre il grafico con 'open': qui lo mostriamo nella finestra
        run = staticmethod(apri_immagine)
    A.subprocess = SoloGrafico
    sys.stdout = Uscita()
    server = ThreadingHTTPServer(("127.0.0.1", PORTA), Gestore)
    avvia("ricollega")
    threading.Timer(0.5, apri_finestra).start()
    server.serve_forever()


# ============================================================== la pagina

PAGINA = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FPV Doctor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Architects+Daughter&display=swap" rel="stylesheet">
<style>
:root {
  /* lavagna: fondo grigio scuro, tratto chiaro come il gessetto */
  --fondo: #2B2F33; --pannello: #33383D; --pannello2: #3E444A; --linea: #555C63;
  --inchiostro: #ECE8DF; --tenue: #AAA69D; --primario: #ECE8DF; --primario-testo: #2B2F33;
  --ok: #9FD897; --ok-fondo: #34452F; --att: #F2958B; --att-fondo: #4A2C29;
  --ver: #F0C27A; --ver-fondo: #4A3D26; --nota: #9CC3E6; --nota-fondo: #2D3B48;
  --stick: #F0C27A; --ombra: none;
  --tr: url(#i-tratteggio); --fondo-ic: #33383D;
  --mano: "Architects Daughter", "Chalkboard SE", "Chalkboard", "Noteworthy", sans-serif;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--fondo); color: var(--inchiostro);
  font: 14.5px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-variant-numeric: tabular-nums;
  display: grid; grid-template-columns: 320px 1fr; height: 100vh;
}
button { font: inherit; color: inherit; cursor: pointer; }
button:focus-visible, input:focus-visible { outline: 3px solid var(--primario); outline-offset: 2px; }

/* ---------------- colonna sinistra */
aside {
  background: var(--pannello); border-right: 1px solid var(--linea);
  display: flex; flex-direction: column; overflow-y: auto;
}
.drone { padding: 18px 20px 14px; border-bottom: 1px solid var(--linea); }
.drone .nome { font-size: 20px; font-weight: 700; letter-spacing: -0.01em; line-height: 1.2; }
.drone .dett { color: var(--tenue); font-size: 13px; margin-top: 4px; }
.riga-stato { display: flex; align-items: center; justify-content: space-between; margin-top: 10px; }
.stato { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; }
.mini { background: var(--pannello2); border: 1px solid var(--linea); border-radius: 8px; padding: 4px 12px; font-size: 12px; font-weight: 600; }
.mini:disabled { opacity: .45; cursor: default; }
.pallino { width: 11px; height: 11px; border-radius: 50%; background: var(--att); }
.stato.on .pallino { background: var(--ok); box-shadow: 0 0 0 4px var(--ok-fondo); }
.azioni { padding: 12px 12px 6px; display: flex; flex-direction: column; gap: 2px; }
.gruppo { font-size: 12px; font-weight: 600; color: var(--tenue); margin: 10px 10px 2px; }
.azione {
  text-align: left; background: none; border: 0; border-radius: 10px; padding: 6px 10px;
  display: block; width: 100%;
}
.azione:hover:not(:disabled) { background: var(--pannello2); }
.azione .t { display: block; font-weight: 600; }
.azione .s { display: block; font-size: 12px; color: var(--tenue); line-height: 1.35; }
.azione:disabled { opacity: .45; cursor: default; }
.azione.principale {
  background: var(--primario); color: var(--primario-testo); padding: 12px 14px; border-radius: 12px;
}
.azione.principale .s { color: inherit; opacity: .85; }
.azione.principale:hover:not(:disabled) { background: var(--primario); filter: brightness(1.08); }
.piede { margin-top: auto; padding: 10px 12px 14px; border-top: 1px solid var(--linea); display: grid; gap: 6px; }
.secondario {
  background: var(--pannello2); border: 1px solid var(--linea); border-radius: 10px; padding: 9px 12px;
  font-size: 13px; font-weight: 600; text-align: left;
}

/* ---------------- area principale */
main { display: flex; flex-direction: column; min-width: 0; height: 100vh; }
header {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 20px 32px; border-bottom: 1px solid var(--linea); background: var(--pannello);
}
header h1 { margin: 0; font-size: 22px; letter-spacing: -0.015em; }
header .sotto { color: var(--tenue); font-size: 13px; }
.ferma {
  background: var(--att-fondo); color: var(--att); border: 1px solid currentColor; border-radius: 10px;
  padding: 9px 16px; font-weight: 700;
}
.ferma[hidden], .mini[hidden] { display: none; }
.ferma { position: relative; z-index: 25; }   /* Interrompi resta cliccabile anche con una domanda aperta */
.destra { display: flex; gap: 10px; align-items: center; }
.avviso {
  display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  padding: 12px 32px; background: var(--nota-fondo); border-bottom: 1px solid var(--linea); font-weight: 600;
}
.avviso[hidden] { display: none; }
.titoletto { margin: 20px 0 8px; font-size: 16px; font-weight: 700; color: var(--inchiostro); }
.corpo { flex: 1; overflow-y: auto; padding: 24px 32px 48px; }
.foglio { max-width: 820px; }
.vuoto { color: var(--tenue); max-width: 560px; margin-top: 8vh; }
.vuoto h2 { color: var(--inchiostro); font-size: 19px; margin: 0 0 8px; }

/* righe della checklist */
.riga { padding: 2px 0; color: var(--tenue); }
.passo-grande {
  margin: 26px 0 10px; font-size: 17px; font-weight: 700; color: var(--inchiostro);
  padding-bottom: 6px; border-bottom: 2px solid var(--linea);
}
.istruzione {
  margin: 12px 0 6px; padding: 10px 14px; border-left: 4px solid var(--primario);
  background: var(--pannello); color: var(--inchiostro); font-weight: 600; border-radius: 0 8px 8px 0;
}
.risp { color: var(--tenue); font-style: italic; padding-left: 14px; }
.esito { display: flex; gap: 10px; align-items: baseline; padding: 4px 0; color: var(--inchiostro); }
.segno {
  flex: none; width: 22px; height: 22px; border-radius: 50%; display: inline-grid; place-items: center;
  font-size: 11px; font-weight: 800; color: var(--fondo); transform: translateY(4px);
}
.k-ok .segno { background: var(--ok); }
.k-att .segno { background: var(--att); }
.k-ver .segno { background: var(--ver); }
.k-nota .segno { background: var(--nota); }
.k-salt .segno { background: var(--tenue); }
.sezione {
  margin: 22px 0 6px; font-weight: 700; font-size: 14px; display: flex; align-items: center; gap: 8px;
}
.sezione.k-ok { color: var(--ok); } .sezione.k-att { color: var(--att); }
.sezione.k-ver { color: var(--ver); } .sezione.k-nota { color: var(--nota); }
.voce { margin: 6px 0 10px; padding: 10px 14px; border-radius: 10px; background: var(--pannello); box-shadow: var(--ombra); border-left: 4px solid var(--linea); }
.voce.k-ok { border-left-color: var(--ok); } .voce.k-att { border-left-color: var(--att); background: var(--att-fondo); }
.voce.k-ver { border-left-color: var(--ver); } .voce.k-nota { border-left-color: var(--nota); }
.voce .t { font-weight: 700; color: var(--inchiostro); }
.voce .p { color: var(--tenue); font-size: 14px; }
.verdetto { margin: 22px 0 8px; padding: 16px 18px; border-radius: 12px; font-size: 16px; font-weight: 700; }
.verdetto.k-ok { background: var(--ok-fondo); color: var(--ok); }
.verdetto.k-att { background: var(--att-fondo); color: var(--att); }
.verdetto.k-ver { background: var(--ver-fondo); color: var(--ver); }
.check { padding: 3px 0; color: var(--inchiostro); display: flex; gap: 10px; }
.check .q { width: 18px; height: 18px; border: 2px solid var(--tenue); border-radius: 4px; flex: none; transform: translateY(4px); }
.grafico { margin: 16px 0; max-width: 100%; border-radius: 10px; border: 1px solid var(--linea); background: #fff; }

/* ---------------- strumenti in tempo reale */
.live { border-top: 1px solid var(--linea); background: var(--pannello); padding: 14px 32px; display: none; }
.live.on { display: block; }
.live-testo { font-size: 15px; font-weight: 600; }
.radio { display: flex; gap: 36px; align-items: flex-end; flex-wrap: wrap; }
.gimbal { text-align: center; color: var(--tenue); font-size: 12px; }
.gimbal svg { display: block; width: 132px; height: 132px; }
.gimbal .cornice { fill: var(--pannello2); stroke: var(--linea); stroke-width: 2; }
.gimbal .croce { stroke: var(--linea); stroke-width: 1.5; }
.gimbal .punto { fill: var(--stick); }
.gimbal .val { font-size: 11px; margin-top: 4px; color: var(--inchiostro); }
.interruttori { display: grid; gap: 6px; min-width: 220px; }
.aux { display: grid; grid-template-columns: 54px 1fr 50px; gap: 8px; align-items: center; font-size: 12px; color: var(--tenue); }
.aux .barra { height: 10px; border-radius: 5px; background: var(--pannello2); border: 1px solid var(--linea); overflow: hidden; }
.aux .barra i { display: block; height: 100%; background: var(--stick); }

/* ---------------- linguaggio: cosa significa / cosa fare, glossario */
.voce .p b.etic { color: var(--inchiostro); font-weight: 700; }
.voce .p.fare { color: var(--inchiostro); }
.gl { background: none; border: 0; padding: 0; font: inherit; color: inherit; cursor: help;
      text-decoration: underline dotted var(--primario); text-underline-offset: 3px; }
.fumetto { position: fixed; z-index: 30; max-width: 320px; background: var(--pannello); color: var(--inchiostro);
           border: 1px solid var(--linea); border-radius: 10px; padding: 10px 12px; font-size: 13.5px;
           box-shadow: 0 8px 30px rgba(0,0,0,.18); }
.fumetto b { display: block; margin-bottom: 3px; }

/* ---------------- componenti */
.comp { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; margin: 12px 0 0; font-size: 13px; }
.comp dt { color: var(--tenue); }
.comp dd { margin: 0; color: var(--inchiostro); font-weight: 600; overflow-wrap: anywhere; }
.nota-radio { margin-top: 8px; font-size: 13px; padding: 6px 9px; border-radius: 8px; }
.nota-radio[hidden] { display: none; }
.nota-radio.ok { background: var(--ok-fondo); color: var(--ok); }
.nota-radio.att { background: var(--att-fondo); color: var(--att); }
.nota-radio.ver { background: var(--ver-fondo); color: var(--ver); }
.mini.largo { margin-top: 10px; width: 100%; }
.letto { font-weight: 600; color: var(--ok); margin-left: 4px; }
.modulo { width: min(640px, 100%); max-height: 88vh; overflow-y: auto; }
.modulo h2 { margin: 0 0 4px; font-size: 19px; }
.modulo .aiuto { margin: 0 0 14px; }
.campi { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; margin-bottom: 16px; }
.campo label { display: block; font-size: 13px; font-weight: 600; color: var(--tenue); margin-bottom: 3px; }
.dialogo.modulo .campo input, .dialogo.modulo .campo select {
  width: 100%; font: inherit; font-size: 14.5px; padding: 7px 9px; border-radius: 8px; border: 1px solid var(--linea);
  background: var(--pannello2); color: var(--inchiostro); margin: 0;
}

/* ---------------- domande */
.contesto { background: var(--pannello2); border-radius: 10px; padding: 10px 12px; margin: 0 0 14px; font-size: 14px; color: var(--tenue); max-height: 34vh; overflow-y: auto; }
.contesto div + div { margin-top: 4px; }
.contesto .primo { color: var(--inchiostro); font-weight: 700; }
.opzioni { display: grid; gap: 6px; margin: 0 0 14px; max-height: 46vh; overflow-y: auto; }
.opz { text-align: left; padding: 9px 12px; border-radius: 10px; border: 1px solid var(--linea); background: var(--pannello2); }
.opz:hover { border-color: var(--primario); }
.opz.pred { border: 2px solid var(--primario); }
.opz b { margin-right: 6px; }
.velo { position: fixed; inset: 0; z-index: 20; background: rgba(10,12,14,.6); display: none; place-items: center; padding: 24px; }
.velo.on { display: grid; }
.dialogo {
  background: var(--pannello); color: var(--inchiostro); border-radius: 16px; padding: 26px 28px 22px;
  width: min(560px, 100%); box-shadow: 0 20px 60px rgba(0,0,0,.3);
}
.dialogo.pronto { width: min(720px, 100%); border-top: 6px solid var(--primario); }
.dialogo.pericolo { border-top: 6px solid var(--att); }
.dialogo .dom { font-size: 16px; font-weight: 600; margin: 0 0 18px; }
.dialogo.pronto .dom { font-size: 22px; line-height: 1.35; font-weight: 700; }
.dialogo .aiuto { color: var(--tenue); font-size: 13px; margin: -8px 0 18px; }
.dialogo input {
  width: 100%; font: inherit; font-size: 16px; padding: 10px 12px; border-radius: 10px;
  border: 2px solid var(--linea); background: var(--pannello2); color: var(--inchiostro); margin-bottom: 18px;
}
.bottoni { display: flex; gap: 10px; justify-content: flex-end; flex-wrap: wrap; }
.btn { border-radius: 10px; padding: 11px 22px; font-weight: 700; border: 1px solid var(--linea); background: var(--pannello2); }
.btn.si { background: var(--primario); color: var(--primario-testo); border-color: transparent; }
.btn.rosso { background: var(--att); color: var(--fondo); border-color: transparent; }
.btn.grande { font-size: 17px; padding: 14px 30px; }

@media (max-width: 760px) {
  .campi { grid-template-columns: 1fr; }
  body { grid-template-columns: 1fr; height: auto; }
  aside { border-right: 0; border-bottom: 1px solid var(--linea); }
  main { height: auto; min-height: 70vh; }
  header, .corpo, .live { padding-left: 16px; padding-right: 16px; }
}
@media (prefers-reduced-motion: no-preference) {
  .dialogo { animation: entra .16s ease-out; }
  @keyframes entra { from { transform: translateY(8px); opacity: 0; } to { transform: none; opacity: 1; } }
}
/* ---------------- estetica a matita: icone, scritte a mano, drone che lavora */
header h1, .drone .nome, .gruppo, .passo-grande, .titoletto, .vuoto h2, .modulo h2 { font-family: var(--mano); font-weight: 400; }
header h1 { font-size: 26px; }
.drone .nome { font-size: 22px; }
.gruppo { font-size: 15px; color: var(--tenue); }
.passo-grande { font-size: 20px; }
.titoletto { font-size: 18px; }
.ic { fill: none; stroke: currentColor; stroke-width: 3.2; stroke-linecap: round; stroke-linejoin: round; flex: none; }
.ic .c { stroke-opacity: .4; stroke-width: 1.1; }
.ic .h { fill: var(--tr); stroke: none; }
.ic .p { fill: var(--fondo-ic); }
.azione { display: flex; align-items: center; gap: 10px; }
.azione[hidden] { display: none; }
.azione .ic { width: 34px; height: 34px; opacity: .92; }
.azione.principale { --fondo-ic: var(--primario); --tr: url(#i-tratteggio-scuro); }
.azione.principale .ic { width: 40px; height: 40px; }
.comp { grid-template-columns: 26px auto 1fr; align-items: center; gap: 4px 8px; }
.comp .ic { width: 26px; height: 26px; color: var(--inchiostro); }
.titolo-riga { display: flex; align-items: center; gap: 12px; }
.girello { width: 46px; height: 46px; flex: none; }
.girello[hidden] { display: none; }
.lavoro { display: none; align-items: center; gap: 22px; padding: 14px 32px; border-top: 1px solid var(--linea); background: var(--pannello); }
.lavoro.on { display: flex; }
.lavoro .grande { width: 110px; height: 110px; flex: none; }
.lavoro .scritta { font-family: var(--mano); font-size: 24px; }
.lavoro .scritta::after { content: ""; display: inline-block; width: 1.4em; text-align: left; animation: puntini 1.6s steps(1) infinite; }
.lavoro .det { color: var(--tenue); font-size: 14px; }
.dr { fill: none; stroke: currentColor; stroke-width: 7; stroke-linecap: round; stroke-linejoin: round; }
.dr .c { stroke-opacity: .35; stroke-width: 2.5; stroke-dasharray: 9 10; }
.dr .h { fill: var(--tr); stroke: none; }
.dr .p { fill: var(--pannello); }
.girello .dr .p { fill: var(--pannello); }
.dr.vivo { width: 170px; height: 170px; }
.assetto { width: 190px; height: 170px; perspective: 700px; display: grid; place-items: center; }
.assetto .piano { transform-style: preserve-3d; transition: transform .12s linear; }
.live-testo .det { color: var(--tenue); font-weight: 400; font-size: 13px; margin-top: 4px; }
.radio { align-items: center; }
@keyframes puntini { 0% { content: ""; } 25% { content: "."; } 50% { content: ".."; } 75% { content: "..."; } }
@keyframes gira { to { transform: rotate(360deg); } }
@keyframes galleggia { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-5px); } }
@media (prefers-reduced-motion: no-preference) {
  .dr .corpo-giro { animation: gira 7s linear infinite; transform-origin: 120px 120px; }
  .girello .dr .corpo-giro { animation-duration: 4s; }
  .dr .el { animation: gira .3s linear infinite; transform-box: fill-box; transform-origin: center; }
  .dr .el.inv { animation-direction: reverse; }
  .lavoro .grande { animation: galleggia 2.6s ease-in-out infinite; }
}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
<defs>
<pattern id="i-tratteggio" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(40)"><line x1="0" y1="0" x2="0" y2="5" stroke="#ECE8DF" stroke-width="1"/></pattern>
<pattern id="i-tratteggio-scuro" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(40)"><line x1="0" y1="0" x2="0" y2="5" stroke="#2B2F33" stroke-width="1"/></pattern>
<filter id="i-matita" x="-10%" y="-10%" width="120%" height="120%">
<feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="2" seed="3" result="n"/>
<feDisplacementMap in="SourceGraphic" in2="n" scale="2.6" xChannelSelector="R" yChannelSelector="G"/>
</filter>
<filter id="i-matita-viva" x="-10%" y="-10%" width="120%" height="120%">
<feTurbulence type="fractalNoise" baseFrequency="0.03" numOctaves="2" seed="3" result="n">
<animate attributeName="seed" values="3;7;12;5" dur=".5s" calcMode="discrete" repeatCount="indefinite"/>
</feTurbulence>
<feDisplacementMap in="SourceGraphic" in2="n" scale="4" xChannelSelector="R" yChannelSelector="G"/>
</filter>
<symbol id="i-motori" viewBox="0 0 120 120"><path class="c" d="M60 4v112M14 38h92"/><path class="h" d="M74 40l14-2v40q-6 6-14 8z"/><path d="M32 38v40a28 9 0 0 0 56 0V38"/><ellipse cx="60" cy="38" rx="28" ry="9"/><path d="M56 20v17M64 20v17"/><path d="M40 47v22M49 49v25M71 49v25M80 47v22"/></symbol>
<symbol id="i-esc" viewBox="0 0 120 120"><path class="c" d="M6 55l90-25"/><path d="M20 52l58-16 22 22-58 18z"/><path d="M20 52v5l22 24 58-18v-5"/><path class="h" d="M30.3 53.7l15-4.2 6 6.5-15.1 4.2zM57 46.3l15-4.1 6 6.4-15.1 4.1zM39.7 64l15.1-4.2 5.9 6.5-15 4.2zM66.4 56.6l15.1-4.1 5.9 6.4-15.1 4.2z"/><path d="M30.3 53.7l15-4.2 6 6.5-15.1 4.2zM57 46.3l15-4.1 6 6.4-15.1 4.1zM39.7 64l15.1-4.2 5.9 6.5-15 4.2zM66.4 56.6l15.1-4.1 5.9 6.4-15.1 4.2z"/><path d="M25 59c-10 6-14 16-16 30M30 65c-8 8-10 18-10 30"/></symbol>
<symbol id="i-scheda" viewBox="0 0 120 120"><path class="c" d="M14 46l90 14M10 52l78-30"/><path d="M24 48l48-18 26 28-48 20z"/><path d="M24 48v4l26 30 48-20v-4"/><path class="h" d="M49.9 52.2l14.4-5.4 7.8 9-14.4 5.4z"/><path d="M49.9 52.2l14.4-5.4 7.8 9-14.4 5.4z"/><ellipse cx="31.4" cy="49.2" rx="3" ry="2"/><ellipse cx="69.8" cy="34.8" rx="3" ry="2"/><ellipse cx="90.6" cy="58.8" rx="3" ry="2"/><ellipse cx="52.2" cy="73.2" rx="3" ry="2"/></symbol>
<symbol id="i-ricevente" viewBox="0 0 120 120"><path class="c" d="M12 80h96"/><path class="h" d="M70 62l8-6v18l-8 6z"/><path d="M44 62h26v18H44zM44 62l8-6h26l-8 6M70 62l8-6v18l-8 6"/><path d="M54 59c-4-14-14-22-24-28M66 59c6-14 18-20 26-26M14 32l16-16M84 24l16 16"/></symbol>
<symbol id="i-video" viewBox="0 0 120 120"><path class="c" d="M8 92h104"/><path class="h" d="M62 46l12-6v32l-12 6z"/><path d="M30 46h32v32H30zM30 46l12-6h32l-12 6M62 46l12-6v32l-12 6"/><circle class="p" cx="42" cy="65" r="11"/><circle class="h" cx="42" cy="65" r="6"/><circle cx="42" cy="65" r="6"/><path d="M90 86V42M86 92h8v-6h-8zM80 36q10-18 20 0"/><ellipse cx="90" cy="37" rx="10" ry="3.5"/><path class="c" d="M76 22c4-6 10-8 14-8s10 2 14 8"/></symbol>
<symbol id="i-radio" viewBox="0 0 120 120"><path class="c" d="M6 66h108"/><path d="M18 56q-2-10 8-12h68q10 2 8 12l-4 32q-2 8-12 8H34q-10 0-12-8z"/><path d="M44 44q0-14 16-14t16 14M26 44l-4-10M94 44l4-10"/><circle cx="38" cy="66" r="11"/><circle cx="82" cy="66" r="11"/><path d="M38 66l-4-12M82 66l5-11"/><path class="h" d="M53 58h14v10H53z"/><path d="M53 58h14v10H53z"/></symbol>
<symbol id="i-frame" viewBox="0 0 120 120"><ellipse class="c" cx="60" cy="62" rx="54" ry="30"/><path d="M22 82L86 36M98 82L34 36"/><ellipse class="p" cx="34" cy="36" rx="9" ry="4"/><ellipse class="p" cx="86" cy="36" rx="9" ry="4"/><ellipse class="p" cx="22" cy="82" rx="11" ry="5"/><ellipse class="p" cx="98" cy="82" rx="11" ry="5"/><path class="p" d="M48 58l12-6 12 6v8l-12 6-12-6z"/><path class="h" d="M60 64l12-6v8l-12 6z"/><path d="M48 58l12-6 12 6-12 6zM48 58v8l12 6 12-6v-8M60 64v8"/></symbol>
<symbol id="i-eliche" viewBox="0 0 120 120"><ellipse class="c" cx="60" cy="62" rx="52" ry="24"/><g transform="translate(60 62) scale(1 .5)"><path class="h" d="M0 0C6-10 10-30 4-46c-6-4-12 2-12 10C-8-22-4-8 0 0z"/><path vector-effect="non-scaling-stroke" d="M0 0C6-10 10-30 4-46c-6-4-12 2-12 10C-8-22-4-8 0 0z"/><path vector-effect="non-scaling-stroke" d="M0 0C6-10 10-30 4-46c-6-4-12 2-12 10C-8-22-4-8 0 0z" transform="rotate(120)"/><path vector-effect="non-scaling-stroke" d="M0 0C6-10 10-30 4-46c-6-4-12 2-12 10C-8-22-4-8 0 0z" transform="rotate(240)"/></g><path class="p" d="M54 62v-6a6 2.4 0 0 1 12 0v6a6 2.4 0 0 1-12 0z"/><ellipse cx="60" cy="56" rx="6" ry="2.4"/></symbol>
<symbol id="i-batteria" viewBox="0 0 120 120"><path class="c" d="M6 84h108"/><path class="h" d="M82 52l14-10v32l-14 10z"/><path d="M22 52h60v32H22zM22 52l14-10h60l-14 10M82 52l14-10v32l-14 10"/><path d="M48 52v32M28 62h14M28 68h10"/><path d="M92 60c12 0 16 10 12 20M98 80h12v8H98z"/><path d="M28 50c-6-8-10-16-6-26M18 18h14v6H18z"/></symbol>
<symbol id="i-gps" viewBox="0 0 120 120"><path class="c" d="M10 70h100"/><path class="h" d="M60 78l32-14v8l-32 14z"/><path d="M28 64l32-14 32 14-32 14zM28 64v8l32 14 32-14v-8M60 78v8"/><path class="h" d="M48 64l12-5 12 5-12 5z"/><path d="M48 64l12-5 12 5-12 5z"/><path d="M86 28l8-8 6 6-8 8zM84 20l-6-6 4-4 6 6M96 34l6 6 4-4-6-6"/><path class="c" d="M84 32L66 56M90 36L74 58"/></symbol>
<symbol id="i-analisi" viewBox="0 0 120 120"><path class="c" d="M6 41l100-14"/><path d="M20 40l66-10 14 54-66 12z"/><path class="c" d="M28 82l60-10M28 82l-6-34"/><path d="M30 74l8-12 6 8 10-20 8 14 10-8 8 10"/><g transform="rotate(-45 86 86)"><path class="p" d="M62 82h38v8H62z"/><path class="h" d="M62 86h38v4H62z"/><path d="M62 82h38v8H62zM100 82l10 4-10 4M62 82h-6v8h6"/></g></symbol>
<symbol id="i-officina" viewBox="0 0 120 120"><g transform="rotate(-40 60 60)"><path class="p" d="M14 56h66v8H14z"/><path d="M18 56h62M18 64h62M18 56a4 4 0 0 0 0 8M80 60a12 12 0 1 1 24 0M80 60a12 12 0 0 0 24 0M94 55h11M94 65h11M94 55v10"/></g><g transform="rotate(40 60 60)"><path class="h" d="M16 53h36v14H16z"/><path d="M21 53h31v14H21a5 5 0 0 1 0-14zM28 53v14M36 53v14M44 53v14"/><path class="p" d="M52 58h42v4H52z"/><path d="M52 58h42M52 62h42M94 58l6 2-6 2"/></g></symbol>
<symbol id="i-scarica" viewBox="0 0 120 120"><path class="c" d="M8 92h104"/><path class="h" d="M84 58l10-8v28l-10 8z"/><path d="M26 58h58v28H26zM26 58l10-8h58l-10 8M84 58l10-8v28l-10 8"/><path d="M58 12v34M46 34l12 12 12-12"/><path d="M34 72h20"/></symbol>
<symbol id="i-gomma" viewBox="0 0 120 120"><path class="c" d="M8 92h104"/><g transform="rotate(-30 60 60)"><path class="p" d="M30 48h60v24H30z"/><path class="h" d="M30 48h24v24H30z"/><path d="M30 48h60v24H30zM54 48v24M30 72l6 6h60l-6-6M90 48l6 6v24"/></g><path class="c" d="M20 100c10-4 22-4 34 0s22 4 34 0"/></symbol>
<symbol id="i-libro" viewBox="0 0 120 120"><path class="c" d="M8 92h104"/><path d="M60 38c-12-8-30-10-44-6v52c14-4 32-2 44 6 12-8 30-10 44-6V32c-14-4-32-2-44 6zM60 38v52"/><path class="h" d="M66 44c8-5 20-6 30-4v10c-10-2-22-1-30 4z"/><path d="M24 46c8-2 18-1 26 3M24 56c8-2 18-1 26 3M24 66c8-2 18-1 26 3M66 66c8-4 20-5 30-3"/></symbol>
<symbol id="i-livella" viewBox="0 0 120 120"><path class="c" d="M6 80h108"/><path class="h" d="M96 52l8-6v20l-8 6z"/><path d="M14 52h82v20H14zM14 52l8-6h82l-8 6M96 52l8-6v20l-8 6"/><path class="p" d="M42 56h26v12H42z"/><path d="M42 56h26v12H42zM50 56v12M60 56v12"/><circle cx="55" cy="62" r="3.5"/></symbol>
<symbol id="i-vibra" viewBox="0 0 120 120"><path class="c" d="M60 10v100"/><path class="h" d="M70 44l12-2v30q-4 5-12 6z"/><path d="M38 42v30a22 7 0 0 0 44 0V42"/><ellipse cx="60" cy="42" rx="22" ry="7"/><path d="M57 26v15M63 26v15"/><path d="M22 40l-6 6 6 6-6 6 6 6M98 40l6 6-6 6 6 6-6 6"/><path class="c" d="M12 36l-6 10 6 10M108 36l6 10-6 10"/></symbol>
</defs>
</svg>
<aside>
  <div class="drone">
    <div class="nome" id="dNome">Nessun drone</div>
    <div class="dett" id="dDett">Collega il drone con il cavo USB</div>
    <div class="riga-stato">
      <div class="stato" id="dStato"><span class="pallino"></span><span id="dStatoT">Non collegato</span></div>
      <button class="mini" data-a="ricollega">Ricollega</button>
    </div>
    <dl class="comp" id="dComp"></dl>
    <div class="nota-radio" id="dRadio" hidden></div>
    <button class="mini largo" id="bComp" hidden>Componenti</button>
  </div>
  <nav class="azioni" aria-label="Operazioni">
    <button class="azione principale" data-a="officina"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-frame"/></svg><span><span class="t">Controllo in officina</span><span class="s">Tutti i controlli al banco in sequenza, con riepilogo finale</span></span></button>
    <div class="gruppo">Singoli controlli</div>
    <button class="azione" data-a="configurazione"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-scheda"/></svg><span><span class="t">Configurazione</span><span class="s">Solo cavo USB</span></span></button>
    <button class="azione" data-a="ricevente"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-ricevente"/></svg><span><span class="t">Ricevente</span><span class="s">Radio accesa, niente batteria</span></span></button>
    <button class="azione" data-a="esc"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-esc"/></svg><span><span class="t">ESC e batteria</span><span class="s">Batteria attaccata, senza eliche</span></span></button>
    <div class="gruppo">Prove al banco</div>
    <button class="azione" data-a="motori"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-motori"/></svg><span><span class="t">Motori: ordine e verso</span><span class="s">Batteria attaccata, SENZA eliche</span></span></button>
    <button class="azione" data-a="assetto"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-livella"/></svg><span><span class="t">Assetto della scheda</span><span class="s">Muovi il drone, il disegno ti segue</span></span></button>
    <button class="azione" data-a="vibrazioni"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-vibra"/></svg><span><span class="t">Vibrazioni dei motori</span><span class="s">Batteria attaccata, SENZA eliche</span></span></button>
    <div class="gruppo">Dopo il volo</div>
    <button class="azione" data-a="scarica"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-scarica"/></svg><span><span class="t">Scarica il blackbox</span><span class="s">Copia i voli sul Mac</span></span></button>
    <button class="azione" data-a="analisi"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-analisi"/></svg><span><span class="t">Analizza un volo</span><span class="s">Rumore, motori, batteria</span></span></button>
    <button class="azione" data-a="cancella"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-gomma"/></svg><span><span class="t">Cancella i log dal drone</span><span class="s">Dopo averli scaricati</span></span></button>
    <div class="gruppo">Cosa non va</div>
    <button class="azione" data-a="modifica"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-officina"/></svg><span><span class="t">Correggi e migliora</span><span class="s">Diagnosi completa e correzioni guidate</span></span></button>
    <button class="azione" data-a="riferimenti"><svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-libro"/></svg><span><span class="t">Riferimenti Betaflight</span><span class="s">Confronto con i preset di droni simili</span></span></button>
  </nav>
</aside>

<main>
  <header>
    <div class="titolo-riga"><span class="girello" id="girello" hidden><svg class="dr" viewBox="0 0 240 240" aria-hidden="true"><g filter="url(#i-matita-viva)"><g class="corpo-giro">
<path d="M64 64L176 176M176 64L64 176" stroke-width="16"/><path d="M64 64L176 176M176 64L64 176" style="stroke:var(--pannello)" stroke-width="8"/>
<circle class="c" cx="62" cy="62" r="44"/><circle class="c" cx="178" cy="62" r="44"/><circle class="c" cx="62" cy="178" r="44"/><circle class="c" cx="178" cy="178" r="44"/>
<circle class="p" cx="62" cy="62" r="16"/><circle class="h" cx="62" cy="62" r="16"/><circle cx="62" cy="62" r="16"/>
<circle class="p" cx="178" cy="62" r="16"/><circle class="h" cx="178" cy="62" r="16"/><circle cx="178" cy="62" r="16"/>
<circle class="p" cx="62" cy="178" r="16"/><circle class="h" cx="62" cy="178" r="16"/><circle cx="62" cy="178" r="16"/>
<circle class="p" cx="178" cy="178" r="16"/><circle class="h" cx="178" cy="178" r="16"/><circle cx="178" cy="178" r="16"/>
<path class="el" d="M22 62q20-8 40 0q20 8 40 0" stroke-width="6"/><path class="el inv" d="M138 62q20-8 40 0q20 8 40 0" stroke-width="6"/>
<path class="el inv" d="M22 178q20-8 40 0q20 8 40 0" stroke-width="6"/><path class="el" d="M138 178q20-8 40 0q20 8 40 0" stroke-width="6"/>
<rect class="p" x="98" y="86" width="44" height="72" rx="10"/><rect class="h" x="104" y="104" width="32" height="44" rx="4"/><rect x="98" y="86" width="44" height="72" rx="10"/>
<rect class="p" x="106" y="70" width="28" height="20" rx="4"/><rect x="106" y="70" width="28" height="20" rx="4"/><circle cx="120" cy="72" r="7"/>
<path d="M120 158v18"/><circle cx="120" cy="180" r="5"/>
</g></g></svg></span><div>
      <h1 id="titolo">FPV Doctor</h1>
      <div class="sotto" id="sotto">Scegli un controllo dalla colonna a sinistra</div>
    </div></div>
    <div class="destra">
      <button class="ferma" id="bFerma" hidden>Interrompi</button>
      <button class="mini" id="bEsci">Chiudi FPV Doctor</button>
    </div>
  </header>
  <div class="avviso" id="avviso" hidden>
    <span>Vuoi aprire FPV Doctor con un doppio clic, senza Terminale?</span>
    <button class="btn si" id="bIcona">Crea l'icona sulla Scrivania</button>
  </div>
  <div class="corpo" id="corpo">
    <div class="foglio" id="foglio">
      <div class="vuoto">
        <h2>Pronto</h2>
        Collega il drone con il cavo USB e scegli un controllo. Prima di uscire a volare usa il
        Controllo in officina: ti guida passo per passo e alla fine ti dice se il drone è pronto per il campo.
      </div>
    </div>
  </div>
  <div class="lavoro" id="lavoro"><span class="grande"><svg class="dr" viewBox="0 0 240 240" aria-hidden="true"><g filter="url(#i-matita-viva)"><g class="corpo-giro">
<path d="M64 64L176 176M176 64L64 176" stroke-width="16"/><path d="M64 64L176 176M176 64L64 176" style="stroke:var(--pannello)" stroke-width="8"/>
<circle class="c" cx="62" cy="62" r="44"/><circle class="c" cx="178" cy="62" r="44"/><circle class="c" cx="62" cy="178" r="44"/><circle class="c" cx="178" cy="178" r="44"/>
<circle class="p" cx="62" cy="62" r="16"/><circle class="h" cx="62" cy="62" r="16"/><circle cx="62" cy="62" r="16"/>
<circle class="p" cx="178" cy="62" r="16"/><circle class="h" cx="178" cy="62" r="16"/><circle cx="178" cy="62" r="16"/>
<circle class="p" cx="62" cy="178" r="16"/><circle class="h" cx="62" cy="178" r="16"/><circle cx="62" cy="178" r="16"/>
<circle class="p" cx="178" cy="178" r="16"/><circle class="h" cx="178" cy="178" r="16"/><circle cx="178" cy="178" r="16"/>
<path class="el" d="M22 62q20-8 40 0q20 8 40 0" stroke-width="6"/><path class="el inv" d="M138 62q20-8 40 0q20 8 40 0" stroke-width="6"/>
<path class="el inv" d="M22 178q20-8 40 0q20 8 40 0" stroke-width="6"/><path class="el" d="M138 178q20-8 40 0q20 8 40 0" stroke-width="6"/>
<rect class="p" x="98" y="86" width="44" height="72" rx="10"/><rect class="h" x="104" y="104" width="32" height="44" rx="4"/><rect x="98" y="86" width="44" height="72" rx="10"/>
<rect class="p" x="106" y="70" width="28" height="20" rx="4"/><rect x="106" y="70" width="28" height="20" rx="4"/><circle cx="120" cy="72" r="7"/>
<path d="M120 158v18"/><circle cx="120" cy="180" r="5"/>
</g></g></svg></span>
    <div><div class="scritta" id="lavoroT">Sto elaborando</div><div class="det" id="lavoroD"></div></div></div>
  <div class="live" id="live"></div>
</main>

<div class="velo" id="velo" role="dialog" aria-modal="true">
  <div class="dialogo" id="dialogo"></div>
</div>
<div class="velo" id="veloComp" role="dialog" aria-modal="true" aria-labelledby="titoloComp">
  <form class="dialogo modulo" id="modulo"></form>
</div>

<script>
let sessione = -1, ricevute = 0, sezioneCorrente = "", richiestaId = null, ultimaVoce = null;
const $ = (id) => document.getElementById(id);
const esc = (t) => t.replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

const CLASSI = {"✅":"k-ok","⚠️":"k-att","❌":"k-att","🔍":"k-ver","ℹ️":"k-nota","⏭️":"k-salt"};
const SEGNI = {"k-ok":"✓","k-att":"!","k-ver":"?","k-nota":"i","k-salt":"–"};
const SEZIONI = {"DA CONTROLLARE":"k-att","DA VERIFICARE":"k-ver","NOTE":"k-nota","TUTTO A POSTO":"k-ok",
  "DA RISOLVERE":"k-att","MIGLIORIE POSSIBILI":"k-nota","CONTROLLI DA FARE":"k-ver"};

let GLOSS = {}, RE_GLOSS = null;
fetch("/api/glossario").then((r) => r.json()).then((g) => {
  GLOSS = g;
  const termini = Object.keys(g).sort((a, b) => b.length - a.length).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  RE_GLOSS = new RegExp(`(^|[^\\wÀ-ú])(${termini.join("|")})(?=$|[^\\wÀ-ú])`, "gi");
});
function chiave(t) { return Object.keys(GLOSS).find((k) => k.toLowerCase() === t.toLowerCase()); }
function conGlossario(testo) {
  const sicuro = esc(testo);
  if (!RE_GLOSS) return sicuro;
  const visti = new Set();
  return sicuro.replace(RE_GLOSS, (m, prima, termine) => {
    const k = chiave(termine);
    if (!k || visti.has(k)) return m;   // ogni parola spiegata una volta per riga
    visti.add(k);
    return `${prima}<button class="gl" type="button" data-k="${esc(k)}">${termine}</button>`;
  });
}
document.addEventListener("click", (e) => {
  document.querySelectorAll(".fumetto").forEach((f) => f.remove());
  const b = e.target.closest(".gl");
  if (!b) return;
  const f = document.createElement("div"); f.className = "fumetto";
  f.innerHTML = `<b>${esc(b.dataset.k)}</b>${esc(GLOSS[b.dataset.k] || "")}`;
  document.body.appendChild(f);
  const r = b.getBoundingClientRect();
  f.style.left = Math.min(r.left, innerWidth - f.offsetWidth - 12) + "px";
  f.style.top = (r.bottom + 6 + f.offsetHeight > innerHeight ? r.top - f.offsetHeight - 6 : r.bottom + 6) + "px";
  e.stopPropagation();
});

function classeDa(t) {
  for (const e in CLASSI) if (t.startsWith(e)) return [CLASSI[e], t.slice(e.length).trim()];
  return [null, t];
}

function aggiungi(riga) {
  const f = $("foglio");
  const t = riga.trim();
  if (/^[=\-#]{5,}$/.test(t)) return;
  let el;
  let m = t.match(/^#\s*PASSO (\d) DI (\d): (.*)$/);
  if (m) { el = document.createElement("div"); el.className = "passo-grande";
           el.textContent = `Passo ${m[1]} di ${m[2]} · ${m[3].charAt(0) + m[3].slice(1).toLowerCase()}`;
           f.appendChild(el); sezioneCorrente = ""; return; }
  if (t.startsWith("▶")) { el = document.createElement("div"); el.className = "istruzione";
           el.textContent = t.replace(/^▶\s*/, ""); f.appendChild(el); return; }
  if (t.startsWith("↳")) { el = document.createElement("div"); el.className = "risp";
           el.textContent = t.replace(/^↳\s*/, "Risposta: "); f.appendChild(el); return; }
  const [k, resto] = classeDa(t);
  if (k && SEZIONI[resto]) { sezioneCorrente = SEZIONI[resto]; el = document.createElement("div");
           el.className = "sezione " + k; el.textContent = resto.charAt(0) + resto.slice(1).toLowerCase();
           f.appendChild(el); return; }
  if (k && /^(TEST |PRONTO|NON PRONTO)/.test(resto)) { el = document.createElement("div");
           el.className = "verdetto " + (resto.startsWith("PRONTO CON") ? "k-ver" : k);
           el.textContent = resto; f.appendChild(el); return; }
  if (t.startsWith("•")) { ultimaVoce = document.createElement("div");
           ultimaVoce.className = "voce " + sezioneCorrente;
           ultimaVoce.innerHTML = `<div class="t">${conGlossario(t.slice(1).trim())}</div>`;
           f.appendChild(ultimaVoce); return; }
  if (ultimaVoce && /^ {4}\S/.test(riga) && ultimaVoce === f.lastElementChild) {
           const p = document.createElement("div"); p.className = "p";
           const m2 = t.match(/^(Cosa significa|Cosa fare|Trovato da):\s*(.*)$/);
           if (m2) { if (m2[1] === "Cosa fare") p.classList.add("fare");
                     p.innerHTML = `<b class="etic">${m2[1]}:</b> ${conGlossario(m2[2])}`; }
           else p.innerHTML = conGlossario(t);
           ultimaVoce.appendChild(p); return; }
  if (k) { el = document.createElement("div"); el.className = "esito " + k;
           el.innerHTML = `<span class="segno">${SEGNI[k]}</span><span>${conGlossario(resto)}</span>`;
           f.appendChild(el); return; }
  m = t.match(/^\d+\.\s*\[ \]\s*(.*)$/);
  if (m) { el = document.createElement("div"); el.className = "check";
           el.innerHTML = `<span class="q"></span><span>${conGlossario(m[1])}</span>`; f.appendChild(el); return; }
  if (/^[A-ZÀ-Ú0-9 '.:]{8,}$/.test(t) && /[A-Z]{4}/.test(t) && !/^UART\d/.test(t)) { el = document.createElement("div");
           el.className = "titoletto"; el.textContent = t.charAt(0) + t.slice(1).toLowerCase().replace(/ esc\b/, " ESC").replace(/betaflight/g, "Betaflight").replace(/ pid\b/g, " PID");
           f.appendChild(el); return; }
  el = document.createElement("div"); el.className = "riga"; el.innerHTML = conGlossario(t); f.appendChild(el);
}

function gimbal(nome, x, y, etichetta) {
  const px = 16 + (Math.min(Math.max(x, 1000), 2000) - 1000) / 1000 * 100;
  const py = 116 - (Math.min(Math.max(y, 1000), 2000) - 1000) / 1000 * 100;
  return `<div class="gimbal"><svg viewBox="0 0 132 132" aria-label="${nome}">
    <rect class="cornice" x="6" y="6" width="120" height="120" rx="14"/>
    <line class="croce" x1="66" y1="16" x2="66" y2="116"/><line class="croce" x1="16" y1="66" x2="116" y2="66"/>
    <circle class="punto" cx="${px}" cy="${py}" r="9"/></svg>
    <div>${nome}</div><div class="val">${etichetta}</div></div>`;
}

let yaw0 = null;
// drone visto dall'alto, telecamera in alto. Motori Betaflight: 1 dietro dx, 2 davanti dx, 3 dietro sx, 4 davanti sx
function droneAlto(acceso, dubbio) {
  const pos = {4: [62, 62], 2: [178, 62], 3: [62, 178], 1: [178, 178]};
  const motori = Object.entries(pos).map(([n, [x, y]]) => {
    const on = +n === acceso;
    return `<circle class="c" cx="${x}" cy="${y}" r="44"/>
      <circle cx="${x}" cy="${y}" r="17" style="fill:${on ? "var(--ver)" : "var(--pannello)"}"/>
      ${on ? `<path class="el" d="M${x - 40} ${y}q20-8 40 0q20 8 40 0" stroke-width="6"/>` : ""}
      <text x="${x}" y="${y + 7}" text-anchor="middle" style="font: 700 20px var(--mano); fill:${on ? "var(--fondo)" : "currentColor"}; stroke:none">${dubbio ? "?" : n}</text>`;
  }).join("");
  return `<svg class="dr vivo" viewBox="0 0 240 240" aria-hidden="true"><g filter="url(#i-matita)">
    <path d="M64 64L176 176M176 64L64 176" stroke-width="16"/><path d="M64 64L176 176M176 64L64 176" style="stroke:var(--pannello)" stroke-width="8"/>
    ${motori}
    <rect x="98" y="92" width="44" height="60" rx="10" style="fill:var(--pannello)"/><rect class="h" x="104" y="104" width="32" height="40" rx="4"/>
    <rect x="106" y="72" width="28" height="20" rx="4" style="fill:var(--pannello)"/><circle cx="120" cy="76" r="6"/>
    <text x="120" y="22" text-anchor="middle" style="font: 18px var(--mano); fill:currentColor; stroke:none">davanti</text></g></svg>`;
}
function mostraLive(s) {
  const box = $("live");
  if (!s) { box.classList.remove("on"); box.innerHTML = ""; return; }
  box.classList.add("on");
  const mm = s.match(/^MOTORE (\S+)/);
  if (mm) { box.innerHTML = `<div class="radio">${droneAlto(+mm[1] || 0, mm[1] === "?")}
    <div class="live-testo">${mm[1] === "?" ? "Quale motore sta girando?" : "Hai indicato il motore " + mm[1] + " (numerazione di Betaflight)"}</div></div>`; return; }
  const ma = s.match(/^ASSETTO (-?[\d.]+) (-?[\d.]+) (-?\d+)/);
  if (ma) {
    const [r, p, y] = ma.slice(1, 4).map(Number);
    if (yaw0 === null) yaw0 = y;
    const dy = ((y - yaw0 + 540) % 360) - 180;
    box.innerHTML = `<div class="radio"><div class="assetto"><div class="piano" style="transform: rotateX(55deg) rotateZ(${dy}deg) rotateX(${p}deg) rotateY(${r}deg)">${droneAlto(0, false)}</div></div>
      <div class="live-testo">Rollio ${r.toFixed(0)}° · Beccheggio ${p.toFixed(0)}° · Direzione ${dy.toFixed(0)}°
      <div class="det">Il disegno segue il drone: davanti è la telecamera.</div></div></div>`;
    return;
  }
  yaw0 = null;
  const m = s.match(/^R(\d+) P(\d+) Y(\d+) G(\d+)(.*)$/);
  if (!m) { box.innerHTML = `<div class="live-testo">${esc(s)}</div>`; return; }
  const [R, P, Y, G] = m.slice(1, 5).map(Number);
  const aux = [...m[5].matchAll(/A(\d+):(\d+)/g)].map((a) => [a[1], +a[2]]);
  box.innerHTML = `<div class="radio">
    ${gimbal("Stick sinistro", Y, G, `Yaw ${Y} · Gas ${G}`)}
    ${gimbal("Stick destro", R, P, `Roll ${R} · Pitch ${P}`)}
    <div class="interruttori">${aux.map(([n, v]) => `<div class="aux"><span>AUX${n}</span>
      <span class="barra"><i style="width:${Math.max(0, Math.min(100, (v - 1000) / 10))}%"></i></span><span>${v}</span></div>`).join("")}</div>
  </div>`;
}

function mostraDomanda(r) {
  const velo = $("velo"), d = $("dialogo");
  if (!r) { velo.classList.remove("on"); richiestaId = null; return; }
  if (r.id === richiestaId) return;
  richiestaId = r.id;
  d.className = "dialogo";
  if (r.tipo === "pronto") {
    d.classList.add("pronto");
    d.innerHTML = `<p class="dom">${esc(r.testo)}</p>
      <p class="aiuto">Premi il pulsante, poi esegui il comando e tienilo finché l'app non conferma.</p>
      <div class="bottoni"><button class="btn si grande" id="ok">Sono pronto</button></div>`;
    $("ok").onclick = () => rispondi("");
  } else if (r.tipo === "sino") {
    d.innerHTML = `${contesto(r)}<p class="dom">${esc(r.testo)}</p>
      <div class="bottoni"><button class="btn" id="no">No</button><button class="btn si" id="si">Sì</button></div>`;
    $("si").onclick = () => rispondi("s"); $("no").onclick = () => rispondi("n");
  } else {
    const pericolo = r.tipo === "cancella";
    if (pericolo) d.classList.add("pericolo");
    const scelte = (r.opzioni || []).map((o) => `<button class="opz${o.valore === r.predefinito ? " pred" : ""}" data-v="${esc(o.valore)}">
        <b>${o.etichetta.startsWith("Log") ? "Log " + esc(o.valore) : esc(o.valore) + "."}</b> ${esc(o.etichetta.replace(/^Log \d+:\s*/, ""))}</button>`).join("");
    d.innerHTML = `${contesto(r)}<p class="dom">${esc(r.testo)}</p>
      ${scelte ? `<div class="opzioni">${scelte}</div>` : ""}
      ${pericolo ? '<p class="aiuto">La cancellazione e\' definitiva. Scrivi CANCELLA in maiuscolo per confermare.</p>' : ""}
      ${r.predefinito ? `<p class="aiuto">Lascia vuoto per usare ${esc(r.predefinito)}.</p>` : ""}
      <input id="campo" autocomplete="off" spellcheck="false">
      <div class="bottoni"><button class="btn" id="annulla">Annulla</button>
      <button class="btn ${pericolo ? "rosso" : "si"}" id="ok">${pericolo ? "Cancella i log" : "Conferma"}</button></div>`;
    const campo = $("campo");
    d.querySelectorAll(".opz").forEach((b) => (b.onclick = () => rispondi(b.dataset.v)));
    if (scelte) { campo.placeholder = "oppure scrivi qui il numero"; }
    else setTimeout(() => campo.focus(), 30);
    $("ok").onclick = () => rispondi(campo.value.trim());
    $("annulla").onclick = () => rispondi("");
    campo.onkeydown = (e) => { if (e.key === "Enter") rispondi(campo.value.trim()); };
  }
  velo.classList.add("on");
  const primo = d.querySelector("#si, #ok"); if (primo && r.tipo !== "testo" && r.tipo !== "cancella") primo.focus();
}

function contesto(r) {
  if (!r.contesto || !r.contesto.length) return "";
  return `<div class="contesto">${r.contesto.map((t, i) =>
    `<div class="${i === 0 ? "primo" : ""}">${esc(t.replace(/^▶\s*/, ""))}</div>`).join("")}</div>`;
}

async function apriComponenti() {
  const dati = await (await fetch("/api/componenti")).json();
  const f = $("modulo");
  f.innerHTML = `<h2 id="titoloComp">Componenti del drone</h2>
    <p class="aiuto">Servono ai controlli. I campi segnati "letto dal drone" li ho compilati io leggendo la scheda: puoi correggerli.
    ${dati.protocollo ? `La scheda usa il protocollo ${esc(dati.protocollo)}.` : ""}</p>
    <div class="campi">${dati.campi.map((c) => {
      const v = dati.valori[c.chiave] || "";
      const id = "c_" + c.chiave;
      const ctrl = c.scelte
        ? `<select id="${id}" name="${c.chiave}">${[...new Set([...c.scelte, v])].map((o) =>
            `<option value="${esc(o)}"${o === v ? " selected" : ""}>${esc(o || "—")}</option>`).join("")}</select>`
        : `<input id="${id}" name="${c.chiave}" value="${esc(v)}" placeholder="${esc(c.aiuto)}" autocomplete="off">`;
      const letto = (dati.letti || []).includes(c.chiave) ? ' <span class="letto">letto dal drone</span>' : "";
      return `<div class="campo"><label for="${id}">${esc(c.etichetta)}${letto}</label>${ctrl}</div>`;
    }).join("")}</div>
    <div class="bottoni"><button type="button" class="btn" id="compAnnulla">Annulla</button>
    <button type="submit" class="btn si">Salva i componenti</button></div>`;
  $("compAnnulla").onclick = () => $("veloComp").classList.remove("on");
  f.onsubmit = async (e) => {
    e.preventDefault();
    const valori = Object.fromEntries(new FormData(f).entries());
    await fetch("/api/componenti", {method: "POST", body: JSON.stringify({valori})});
    $("veloComp").classList.remove("on");
    aggiorna();
  };
  $("veloComp").classList.add("on");
  setTimeout(() => f.querySelector("input, select").focus(), 30);
}

const ICONE = {"Motori":"motori","Frame":"frame","Eliche":"eliche","Batteria":"batteria","Radio":"ricevente",
  "Video":"video","Porte":"scheda","ESC":"esc","GPS":"gps","Scheda":"scheda"};
function icona(k) { return `<svg class="ic" filter="url(#i-matita)" aria-hidden="true"><use href="#i-${ICONE[k] || "scheda"}"/></svg>`; }
const FRASI = {"officina":"Sto facendo il controllo in officina","configurazione":"Sto leggendo la configurazione",
  "ricevente":"Sto controllando la ricevente","esc":"Sto controllando ESC e batteria","scarica":"Sto scaricando i voli",
  "analisi":"Sto analizzando il volo","cancella":"Sto cancellando i log","modifica":"Sto preparando la diagnosi",
  "riferimenti":"Sto confrontando con i preset di Betaflight","ricollega":"Sto cercando il drone",
  "motori":"Sto provando i motori","assetto":"Sto leggendo l'assetto","vibrazioni":"Sto ascoltando le vibrazioni"};
let ultimaAzione = "";
function mostraLavoro(s) {
  const occupato = !!s.in_corso;
  $("girello").hidden = !occupato;
  $("lavoro").classList.toggle("on", occupato && !s.richiesta && !s.live);
  $("lavoroT").textContent = FRASI[ultimaAzione] || "Sto elaborando";
  $("lavoroD").textContent = s.titolo || "";
}
function mostraComponenti(c, drone) {
  $("bComp").hidden = !(drone && drone.nome);
  if (!c) { $("dComp").innerHTML = ""; $("dRadio").hidden = true; return; }
  $("dComp").innerHTML = c.vuoto
    ? `<span>${icona("Frame")}</span><dt>Componenti</dt><dd>non ancora indicati</dd>`
    : c.righe.map(([k, v]) => `<span>${icona(k)}</span><dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  if (c.nota) { $("dRadio").hidden = false; $("dRadio").className = "nota-radio " + c.nota[0]; $("dRadio").textContent = c.nota[1]; }
  else $("dRadio").hidden = true;
}

async function rispondi(v) {
  $("velo").classList.remove("on");
  await fetch("/api/risposta", {method: "POST", body: JSON.stringify({valore: v})});
}

async function azione(a) {
  ultimaAzione = a;
  await fetch("/api/azione", {method: "POST", body: JSON.stringify({azione: a})});
  aggiorna();
}

function mostraDrone(d) {
  if (d && d.nome) {
    $("dNome").textContent = d.nome;
    const collegato = d.scheda !== "non collegato";
    $("dDett").textContent = collegato ? `${d.scheda} · Betaflight ${d.versione}` : "Analisi dei voli salvati";
    $("dStato").classList.toggle("on", collegato);
    $("dStatoT").textContent = collegato ? "Collegato" : "Non collegato";
  } else {
    $("dNome").textContent = "Nessun drone";
    $("dDett").textContent = "Collega il drone con il cavo USB";
    $("dStato").classList.remove("on");
    $("dStatoT").textContent = "Non collegato";
  }
}

async function aggiorna() {
  let s;
  try { s = await (await fetch(`/api/stato?da=${ricevute}&sessione=${sessione}`)).json(); }
  catch (e) { $("sotto").textContent = "FPV Doctor è stato chiuso. Riaprilo dall'icona."; return; }
  if (s.sessione !== sessione) {
    sessione = s.sessione; ricevute = 0; sezioneCorrente = ""; ultimaVoce = null;
    $("foglio").innerHTML = "";
  }
  const vicinoFondo = $("corpo").scrollHeight - $("corpo").scrollTop - $("corpo").clientHeight < 80;
  s.righe.forEach(aggiungi);
  ricevute += s.righe.length;
  if (s.immagine && !document.querySelector(".grafico")) {
    const img = document.createElement("img"); img.className = "grafico"; img.alt = "Grafico del volo";
    img.src = "/immagine?" + sessione; $("foglio").appendChild(img);
  }
  if (s.righe.length && vicinoFondo) $("corpo").scrollTop = $("corpo").scrollHeight;
  $("titolo").textContent = s.titolo || "FPV Doctor";
  $("sotto").textContent = s.in_corso ? "Sto lavorando, un momento" : (s.titolo ? "Completato" : "Scegli un controllo dalla colonna a sinistra");
  $("bFerma").hidden = !s.in_corso;
  document.querySelectorAll("[data-a]").forEach((b) => (b.disabled = !!s.in_corso));
  if (!$("avviso").dataset.esito) $("avviso").hidden = s.icona;
  $("bEsci").hidden = !!s.in_corso;
  mostraDrone(s.drone);
  mostraComponenti(s.componenti, s.drone);
  mostraLive(s.live);
  mostraDomanda(s.richiesta);
  mostraLavoro(s);
}

document.querySelectorAll("[data-a]").forEach((b) => (b.onclick = () => azione(b.dataset.a)));
fetch("/api/operazioni").then((r) => r.json()).then((ops) => {
  document.querySelectorAll("[data-a]").forEach((b) => { if (!ops.includes(b.dataset.a)) b.hidden = true; });
}).catch(() => {});
$("bComp").onclick = apriComponenti;
$("bFerma").onclick = () => fetch("/api/interrompi", {method: "POST", body: "{}"});
$("bIcona").onclick = async () => {
  const r = await (await fetch("/api/icona", {method: "POST", body: "{}"})).json();
  $("avviso").dataset.esito = "1";
  $("avviso").innerHTML = r.ok
    ? "<span>Icona creata sulla Scrivania: d'ora in poi apri FPV Doctor con un doppio clic.</span>"
    : `<span>Non sono riuscito a creare l'icona: ${esc(r.errore || "errore sconosciuto")}</span>`;
  setTimeout(() => ($("avviso").hidden = true), 8000);
};
$("bEsci").onclick = async () => {
  await fetch("/api/esci", {method: "POST", body: "{}"});
  document.body.innerHTML = '<div style="padding:40px;font-size:20px">FPV Doctor e\' chiuso. Puoi chiudere questa finestra.</div>';
};
setInterval(aggiorna, 250);
aggiorna();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
