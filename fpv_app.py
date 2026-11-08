"""
FPV Doctor - anteprima
App per controllare i droni Betaflight collegati via USB.

Avvio:   python3 fpv_app.py

Regole di sicurezza dell'app:
 - legge sempre, modifica solo dopo la tua conferma
 - prima di ogni modifica salva una copia della configurazione
 - cancella i log solo dopo la tua conferma scritta
"""

import json
import os
import time as _tempo
import re
import struct
import subprocess
import sys
import time
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("Manca la libreria pyserial:  python3 -m pip install pyserial")
    sys.exit(1)

CARTELLA_DRONI = "droni"
ELENCO = os.path.join(CARTELLA_DRONI, "elenco.txt")

# ============================================================== protocollo MSP v2

MSP_API_VERSION, MSP_FC_VARIANT, MSP_FC_VERSION, MSP_BOARD_INFO = 1, 2, 3, 4
MSP_DATAFLASH_SUMMARY, MSP_DATAFLASH_READ, MSP_DATAFLASH_ERASE = 70, 71, 72
MSP_BLACKBOX_CONFIG = 80
MSP_MOTOR_TELEMETRY = 139
MSP_UID = 160
MSP_ANALOG = 110
MSP_RX_CONFIG = 44
MSP_NAME = 10
MSP_BATTERY_STATE = 130
MSP_MOTOR_CONFIG = 131
MSP_CF_SERIAL_CONFIG = 54
MSP2_COMMON_SERIAL_CONFIG = 0x1009

# Protocollo della ricevente come lo chiama Betaflight (serialrx_provider)
PROTOCOLLI = {0: "SPEKTRUM1024", 1: "SPEKTRUM2048", 2: "SBUS", 3: "SUMD", 4: "SUMH", 5: "XB-B", 6: "XB-B-RJ01",
              7: "IBUS", 8: "JETIEXBUS", 9: "CRSF", 10: "SRXL", 11: "CUSTOM", 12: "FPORT", 13: "SRXL2", 14: "GHST"}
# Quale protocollo usa ogni sistema di trasmissione
SISTEMI_RADIO = {"ELRS": {"CRSF"}, "EXPRESSLRS": {"CRSF"}, "CROSSFIRE": {"CRSF"}, "TRACER": {"CRSF"},
                 "FRSKY": {"SBUS", "FPORT"}, "GHOST": {"GHST"}, "FLYSKY": {"IBUS"}, "SPEKTRUM": {"SRXL2", "SPEKTRUM2048"}}


def crc8(dati):
    crc = 0
    for b in dati:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def leggi_esatto(ser, n, attesa=2):
    dati = b""
    scadenza = time.time() + attesa
    while len(dati) < n and time.time() < scadenza:
        dati += ser.read(n - len(dati))
    if len(dati) < n:
        raise TimeoutError("Il drone non ha risposto in tempo.")
    return dati


def msp(ser, comando, payload=b"", attesa=3):
    corpo = bytes([0]) + struct.pack("<HH", comando, len(payload)) + payload
    ser.write(b"$X<" + corpo + bytes([crc8(corpo)]))
    scadenza = time.time() + attesa
    precedente = b""
    while time.time() < scadenza:
        byte = ser.read(1)
        if not byte:
            continue
        if precedente == b"$" and byte == b"X":
            direzione = leggi_esatto(ser, 1)
            intest = leggi_esatto(ser, 5)
            _, cmd, dim = struct.unpack("<BHH", intest)
            dati = leggi_esatto(ser, dim)
            chk = leggi_esatto(ser, 1)[0]
            if chk != crc8(intest + dati):
                raise IOError("Dati arrivati danneggiati.")
            if direzione == b"!":
                return None
            if cmd == comando:
                return dati
        precedente = byte
    raise TimeoutError(f"Nessuna risposta al comando {comando}.")


# ============================================================== collegamento

def trova_porta(attesa=0):
    scadenza = time.time() + attesa
    while True:
        porte = [p.device for p in list_ports.comports() if "usbmodem" in p.device]
        if porte or time.time() >= scadenza:
            return porte[0] if porte else None
        time.sleep(0.5)


def apri(porta=None):
    porta = porta or trova_porta(attesa=10)
    if not porta:
        raise ConnectionError("Drone non trovato. Controlla il cavo e chiudi Betaflight Configurator.")
    ser = serial.Serial(porta, 115200, timeout=0.2)
    time.sleep(0.4)
    ser.reset_input_buffer()
    return ser


def attendi_riavvio():
    """Dopo l'uscita dalla CLI la scheda si riavvia: aspetta che torni."""
    time.sleep(2.5)
    if not trova_porta(attesa=15):
        raise ConnectionError("La scheda non è tornata dopo il riavvio. Scollega e ricollega il cavo.")
    time.sleep(1)


# ============================================================== CLI di Betaflight

def leggi_fino_al_prompt(ser, attesa=15):
    ricevuto = b""
    scadenza = time.time() + attesa
    ultimo = time.time()
    while time.time() < scadenza:
        pezzo = ser.read(ser.in_waiting or 1)
        if pezzo:
            ricevuto += pezzo
            ultimo = time.time()
        elif ricevuto.rstrip(b" ").endswith(b"#") and time.time() - ultimo > 0.6:
            break
    return ricevuto.decode("ascii", errors="replace").replace("\r", "")


class CLI:
    """Sessione CLI: si apre con 'with CLI() as cli:' e alla fine esce (la scheda si riavvia)."""

    def __init__(self, salva_alla_fine=False):
        self.salva = salva_alla_fine

    def __enter__(self):
        self.ser = apri()
        self.ser.write(b"#")
        benvenuto = leggi_fino_al_prompt(self.ser, attesa=5)
        if "#" not in benvenuto:
            self.ser.close()
            raise ConnectionError("Il drone non è entrato nella CLI.")
        return self

    def comando(self, testo, attesa=15):
        self.ser.write(testo.encode("ascii") + b"\r\n")
        risposta = leggi_fino_al_prompt(self.ser, attesa)
        righe = [r.rstrip() for r in risposta.split("\n")]
        righe = [r for r in righe if r.strip() not in (testo, "#", f"# {testo}")]
        while righe and not righe[-1].strip():
            righe.pop()
        while righe and not righe[0].strip():
            righe.pop(0)
        return righe

    def __exit__(self, *errore):
        try:
            self.ser.write(b"save\r\n" if self.salva else b"exit\r\n")
            time.sleep(0.3)
        finally:
            self.ser.close()
        attendi_riavvio()


# ============================================================== cartelle dei droni

def leggi_elenco():
    elenco = {}
    if os.path.exists(ELENCO):
        for riga in open(ELENCO, encoding="utf-8"):
            if "=" in riga:
                uid, nome = riga.split("=", 1)
                elenco[uid.strip()] = nome.strip()
    return elenco


def registra_drone(uid, nome):
    os.makedirs(CARTELLA_DRONI, exist_ok=True)
    with open(ELENCO, "a", encoding="utf-8") as f:
        f.write(f"{uid} = {nome}\n")


def cartella(nome):
    percorso = os.path.join(CARTELLA_DRONI, nome)
    os.makedirs(percorso, exist_ok=True)
    return percorso


MODELLO_COMPONENTI = """# Componenti del drone: modifica i valori dopo il segno "=".
nome = {nome}
celle_batteria =
tipo_batteria = LiPo
motori =
kv_motori =
poli_motore = 14
eliche =
frame =
esc =
esc_celle_max =
fc_celle_max =
firmware_esc =
ricevitore =
porta_ricevitore =
porte_da_evitare =
vtx =
gps =
satelliti_tipici =
# Impostazioni scelte di proposito (separate da virgola): l'app le mostra come nota.
voluto =
"""


# Valori che chi compila usa per dire "non c'è" o "non lo so": per l'app sono campi vuoti
VUOTI = {"", "/", "//", "-", "--", "no", "nessuno", "nessuna", "n/a", "na", "assente", "x", "?"}


def leggi_componenti(nome):
    percorso = os.path.join(cartella(nome), "componenti.txt")
    comp = {}
    if os.path.exists(percorso):
        for riga in open(percorso, encoding="utf-8"):
            r = riga.strip()
            if r and not r.startswith("#") and "=" in r:
                k, v = r.split("=", 1)
                v = v.strip()
                comp[k.strip().lower()] = "" if v.lower() in VUOTI else v
    return comp


def ha_gps(drone):
    """True/False se si sa dalla scheda, altrimenti dai componenti. La scheda ha sempre l'ultima parola."""
    ril = leggi_rilevati(drone) if drone.get("nome") else {}
    if ril.get("porte") is not None and ril.get("porte") != []:
        return any("GPS" in p for p in ril["porte"])
    diff = sorted(f for f in os.listdir(cartella(drone["nome"])) if f.startswith("diff_"))
    if diff:
        return "feature GPS" in open(os.path.join(cartella(drone["nome"]), diff[-1]), encoding="utf-8").read()
    return bool(leggi_componenti(drone["nome"]).get("gps"))


def chiedi(domanda):
    return input(domanda).strip()


def conferma(domanda):
    return chiedi(domanda + " (s/n): ").lower() in ("s", "si", "sì")


# ============================================================== 0. riconoscere il drone

def riconosci():
    with apri() as ser:
        variante = msp(ser, MSP_FC_VARIANT)
        versione = msp(ser, MSP_FC_VERSION)
        scheda = msp(ser, MSP_BOARD_INFO)
        uid = msp(ser, MSP_UID)
        try:
            rx = msp(ser, MSP_RX_CONFIG)
        except (TimeoutError, IOError):
            rx = None
        try:
            letti = leggi_dal_drone(ser)
        except (TimeoutError, IOError, IndexError, struct.error):
            letti = {}
    if not variante or variante[:4] != b"BTFL":
        raise ConnectionError("Questo dispositivo non sembra una scheda Betaflight.")
    info = {
        "versione": f"{versione[0]}.{versione[1]}.{versione[2]}",
        "uid": uid.hex() if uid else "sconosciuto",
        "scheda": "",
        "protocollo": PROTOCOLLI.get(rx[0]) if rx else None,
    }
    letti["protocollo"] = info["protocollo"]
    try:
        pos = 8
        lung = scheda[pos]; pos += 1 + lung          # target
        lung = scheda[pos]
        info["scheda"] = scheda[pos + 1:pos + 1 + lung].decode("ascii", errors="replace")
    except IndexError:
        pass

    elenco = leggi_elenco()
    if info["uid"] in elenco:
        info["nome"] = elenco[info["uid"]]
    else:
        print()
        print(f"Drone nuovo! Scheda {info['scheda']}, Betaflight {info['versione']}.")
        nome = ""
        while not nome:
            nome = chiedi("Che nome gli diamo? (es. 6 pollici long range): ")
        nome = re.sub(r"[^\w\- ]", "", nome).strip()
        registra_drone(info["uid"], nome)
        percorso = os.path.join(cartella(nome), "componenti.txt")
        if not os.path.exists(percorso):
            open(percorso, "w", encoding="utf-8").write(MODELLO_COMPONENTI.format(nome=nome).replace("poli_motore = 14", "poli_motore ="))
        print("Leggo dal drone quello che posso e compilo i componenti; il resto (motori, frame, eliche) "
              "aggiungilo con il pulsante Componenti.")
        info["nome"] = nome
    info["rilevati"] = compila_dai_dati(info, letti)
    proponi_spostamento(info)
    return info


def leggi_dal_drone(ser):
    """Quello che la scheda sa di se': porte e funzioni, poli dei motori, celle, nome. Solo letture MSP,
    nessun riavvio."""
    letti = {"porte": {}}
    r = None
    try:
        r = msp(ser, MSP2_COMMON_SERIAL_CONFIG)
    except (TimeoutError, IOError):
        r = None
    if r and len(r) >= 1 and len(r) >= 1 + r[0] * 9:
        for i in range(r[0]):
            ident, maschera = struct.unpack("<BI", r[1 + i * 9:6 + i * 9])
            letti["porte"][ident] = maschera
    else:
        r = msp(ser, MSP_CF_SERIAL_CONFIG)
        for i in range(0, len(r or b"") - 6, 7):
            ident, maschera = struct.unpack("<BH", r[i:i + 3])
            letti["porte"][ident] = maschera
    m = msp(ser, MSP_MOTOR_CONFIG)
    if m and len(m) >= 10:
        letti["poli"] = m[7]
        letti["bidir"] = bool(m[8])
    b = msp(ser, MSP_BATTERY_STATE)
    if b and len(b) >= 1 and b[0]:
        letti["celle"] = b[0]
    n = msp(ser, MSP_NAME)
    if n:
        letti["nome_scheda"] = n.decode("ascii", errors="replace").strip("\x00 ")
    st = msp(ser, MSP_STATUS_EX)
    if st and len(st) >= 2:
        ciclo = struct.unpack("<H", st[:2])[0]
        if 50 <= ciclo <= 2000:
            letti["ciclo_us"] = ciclo   # durata di un giro di calcolo dei PID, in microsecondi
    return letti


RICEVENTI_DA_PROTOCOLLO = {"CRSF": "ELRS o Crossfire (CRSF)", "GHST": "Ghost", "SBUS": "SBUS (FrSky o altre)",
                           "FPORT": "FrSky (FPort)", "IBUS": "FlySky (IBUS)", "SRXL2": "Spektrum (SRXL2)"}


def descrivi_dati(letti):
    """Traduce le letture della scheda in valori per i componenti."""
    porte = letti.get("porte", {})
    def porta_con(bit):
        return next((nome_porta(n) for n, mm in sorted(porte.items()) if mm & bit and n != 20), "")
    d = {}
    rx = porta_con(64)
    if rx:
        d["porta_ricevitore"] = rx
    if letti.get("protocollo") in RICEVENTI_DA_PROTOCOLLO:
        d["ricevitore"] = RICEVENTI_DA_PROTOCOLLO[letti["protocollo"]]
    if porta_con(131072):
        d["vtx"] = f"Digitale HD con OSD (DJI, Walksnail o HDZero) su {porta_con(131072)}"
    elif porta_con(2048):
        d["vtx"] = f"Analogico (SmartAudio) su {porta_con(2048)}"
    elif porta_con(8192):
        d["vtx"] = f"Analogico (Tramp) su {porta_con(8192)}"
    if porta_con(2):
        d["gps"] = f"Presente su {porta_con(2)}"
    if letti.get("poli"):
        d["poli_motore"] = str(letti["poli"])
    if letti.get("celle"):
        d["celle_batteria"] = str(letti["celle"])
    return d


def compila_dai_dati(drone, letti):
    """Riempie i campi vuoti dei componenti con quello che ha letto dal drone; segnala le differenze.
    Ritorna un riassunto per la finestra (porte occupate e campi letti dal drone)."""
    if not letti:
        return {}
    dedotti = descrivi_dati(letti)
    attuali = leggi_componenti(drone["nome"])
    da_scrivere, diversi = {}, []
    for chiave, valore in dedotti.items():
        mio = attuali.get(chiave, "").strip()
        if not mio:
            da_scrivere[chiave] = valore
        elif chiave in ("porta_ricevitore", "poli_motore", "celle_batteria") and \
                mio.upper().replace(" ", "") != valore.upper().replace(" ", ""):
            diversi.append(f"{chiave.replace('_', ' ')}: nei componenti '{mio}', il drone dice '{valore}'")
    if da_scrivere:
        scrivi_componenti(drone["nome"], da_scrivere)
    porte = [f"{nome_porta(n)}: {', '.join(funzioni(mm))}" for n, mm in sorted(letti.get("porte", {}).items())
             if mm and n != 20]
    riassunto = {"porte": porte, "letti": sorted(dedotti), "compilati": sorted(da_scrivere), "diversi": diversi,
                 "bidir": letti.get("bidir"), "nome_scheda": letti.get("nome_scheda", ""),
                 "ciclo_us": letti.get("ciclo_us"), "protocollo": letti.get("protocollo")}
    json.dump(riassunto, open(os.path.join(cartella(drone["nome"]), "rilevati.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    if porte:
        print("Letto dal drone: " + "; ".join(porte))
    if da_scrivere:
        print("Ho compilato in automatico nei componenti: " +
              ", ".join(f"{k.replace('_', ' ')} = {v}" for k, v in da_scrivere.items()))
    for d in diversi:
        print(f"⚠️  Da verificare: {d}")
    return riassunto


def leggi_rilevati(drone):
    try:
        return json.load(open(os.path.join(cartella(drone["nome"]), "rilevati.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ============================================================== componenti e file del drone

CAMPI_COMPONENTI = [
    # chiave, etichetta, suggerimento, scelte
    ("motori", "Motori (marca e misura)", "es. GEPRC 2407E", None),
    ("kv_motori", "KV dei motori", "es. 1750", None),
    ("frame", "Frame", "es. FlyFish VD6", None),
    ("eliche", "Eliche", "es. 6 pollici tripala", None),
    ("celle_batteria", "Celle della batteria", "es. 6", ["", "1", "2", "3", "4", "5", "6", "8"]),
    ("tipo_batteria", "Tipo di batteria", "", ["", "LiPo", "LiHV (alta tensione)", "Li-ion"]),
    ("ricevitore", "Trasmissione radio", "", ["", "ELRS 2.4", "ELRS 900", "Crossfire", "Tracer", "FrSky", "Ghost", "FlySky", "Spektrum", "Altro"]),
    ("porta_ricevitore", "Porta della ricevente", "es. UART5", None),
    ("vtx", "Trasmissione video", "es. DJI O4 Pro", None),
    ("esc", "ESC", "es. 4in1 50A", None),
    ("esc_celle_max", "ESC: celle massime", "", ["", "2", "3", "4", "6", "8", "12"]),
    ("fc_celle_max", "Scheda di volo: celle massime", "", ["", "1", "2", "3", "4", "6", "8", "12"]),
    ("firmware_esc", "Firmware ESC", "", ["", "Bluejay", "BLHeli_S", "BLHeli_32", "AM32", "Altro"]),
    ("poli_motore", "Poli dei motori", "di solito 14", None),
    ("gps", "GPS", "es. FlyFish M100", None),
    ("satelliti_tipici", "Satelliti che vedi di solito", "es. 6", None),
    ("porte_da_evitare", "Porte da non usare", "es. UART2", None),
]


def scrivi_componenti(nome, valori):
    """Riscrive componenti.txt con i valori dati, conservando le voci che il modulo non tocca (es. 'voluto')."""
    attuali = leggi_componenti(nome)
    attuali.update({k: str(v).strip() for k, v in valori.items() if k in dict((c[0], 1) for c in CAMPI_COMPONENTI)})
    righe = ["# Componenti del drone (si modificano anche dalla finestra: pulsante Componenti)", f"nome = {nome}"]
    for chiave, etichetta, _, _ in CAMPI_COMPONENTI:
        righe.append(f"{chiave} = {attuali.get(chiave, '')}")
    for chiave, valore in attuali.items():
        if chiave not in ("nome", "voluto") and chiave not in dict((c[0], 1) for c in CAMPI_COMPONENTI):
            righe.append(f"{chiave} = {valore}")
    righe.append("# Impostazioni scelte di proposito (separate da virgola): l'app le mostra come nota.")
    righe.append(f"voluto = {attuali.get('voluto', '')}")
    open(os.path.join(cartella(nome), "componenti.txt"), "w", encoding="utf-8").write("\n".join(righe) + "\n")


def confronta_radio(ricevitore, protocollo):
    """True se il sistema radio scritto nei componenti usa il protocollo letto dalla scheda, False se no,
    None se non si può dire."""
    if not ricevitore or not protocollo:
        return None
    for sistema, protocolli in SISTEMI_RADIO.items():
        if sistema in ricevitore.upper().replace(" ", ""):
            return protocollo in protocolli
    return None


def file_del_drone_fuori_posto(drone):
    """File nella cartella principale che appartengono a questo drone (stessa scheda): configurazioni e blackbox
    salvati dai primi programmi, prima che esistessero le cartelle dei droni."""
    trovati = []
    scheda = drone.get("scheda", "")
    if not scheda:
        return trovati
    for f in sorted(os.listdir(".")):
        try:
            if f.startswith("diff_") and f.endswith(".txt"):
                if f"board_name {scheda}" in open(f, encoding="utf-8", errors="replace").read():
                    trovati.append(f)
            elif f.endswith(".bbl"):
                with open(f, "rb") as bb:
                    testa = bb.read(4096).decode("latin1", errors="replace")
                if scheda in testa:
                    trovati.append(f)
        except OSError:
            pass
    return trovati


def proponi_spostamento(drone):
    fuori = file_del_drone_fuori_posto(drone)
    if not fuori:
        return
    print(f"Nella cartella principale ci sono {len(fuori)} file di questo drone: {', '.join(fuori)}")
    if conferma(f"Li sposto nella cartella di '{drone['nome']}', insieme alle altre sue analisi?"):
        for f in fuori:
            destinazione = os.path.join(cartella(drone["nome"]), f)
            if not os.path.exists(destinazione):
                os.replace(f, destinazione)
        print(f"Spostati nella cartella droni/{drone['nome']}.")


# ============================================================== consigli di modifica

MANOVRE_TARATURA = [
    "Una decina di secondi di volo tranquillo",
    "5-6 colpi secchi di stick sul ROLL, a destra e a sinistra, con ritorno al centro",
    "Lo stesso sul PITCH e sullo YAW",
    "2-3 flip o roll completi",
    "2 punch out (gas a tutta per 1-2 secondi) seguiti da una discesa, per il propwash",
]
PROVA_FILTRI = [
    "Volo normale di 2-3 minuti, con qualche accelerata decisa",
    "Appena atterrato tocca i motori: devono essere tiepidi, non bollenti",
    "Scarica il blackbox e analizza il volo: il rumore dopo i filtri deve restare basso",
]


def file_consigli(drone):
    return os.path.join(cartella(drone["nome"]), "consigli.json")


def leggi_consigli(drone):
    try:
        return json.load(open(file_consigli(drone), encoding="utf-8"))
    except (OSError, ValueError):
        return []


def salva_consigli(drone, origine, nuovi):
    """Sostituisce i consigli di una certa origine ('configurazione' o 'volo') con quelli nuovi."""
    tenuti = [c for c in leggi_consigli(drone) if c.get("origine") != origine]
    data = f"{datetime.now():%d/%m/%Y %H:%M}"
    for c in nuovi:
        c.update({"origine": origine, "data": data})
    json.dump(tenuti + nuovi, open(file_consigli(drone), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def togli_consiglio(drone, parametro):
    rimasti = [c for c in leggi_consigli(drone) if c["param"] != parametro]
    json.dump(rimasti, open(file_consigli(drone), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


FONTI = {"configurazione": "Configurazione", "ricevente": "Test della ricevente",
         "esc": "Test ESC e batteria", "volo": "Analisi del volo"}


def salva_rapporto(drone, tipo, rap, extra=""):
    """Conserva l'esito dell'ultimo controllo di ogni tipo: serve alla diagnosi completa."""
    percorso = os.path.join(cartella(drone["nome"]), "rapporti.json")
    try:
        tutti = json.load(open(percorso, encoding="utf-8"))
    except (OSError, ValueError):
        tutti = {}
    tutti[tipo] = {"data": f"{datetime.now():%d/%m/%Y %H:%M}", "quando": _tempo.time(), "extra": extra,
                   "rap": [[r[0], r[1], r[2]] for r in rap]}
    json.dump(tutti, open(percorso, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def leggi_rapporti(drone):
    try:
        return json.load(open(os.path.join(cartella(drone["nome"]), "rapporti.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# Nomi comprensibili dei parametri che l'app può proporre
NOMI_PARAMETRI = {
    "vbat_max_cell_voltage": "Tensione massima per cella (centesimi di volt)",
    "feature_channel_forwarding": "Funzione Channel forwarding (comando di servi e gimbal)",
    "feature_telemetry": "Telemetria verso la radio",
    "rssi_channel": "Canale da cui leggere il segnale (0 = disabilitato)",
    "osd_rssi_dbm_alarm": "Allarme del segnale RSSI sull'OSD (dBm)",
    "failsafe_delay": "Attesa prima del failsafe (decimi di secondo)",
    "force_battery_cell_count": "Numero di celle fisso (0 = lo calcola la scheda)",
    "vbat_warning_cell_voltage": "Avviso di batteria bassa per cella (centesimi di volt)",
    "vbat_min_cell_voltage": "Batteria scarica per cella (centesimi di volt)",
    "simplified_gyro_filter_multiplier": "Filtro del giroscopio (cursore)",
    "simplified_dterm_filter_multiplier": "Filtro del D (cursore)",
    "simplified_d_gain": "Smorzamento D (cursore)",
    "simplified_pi_gain": "Reattività P e I (cursore)",
    "simplified_feedforward_gain": "Feedforward (cursore)",
    "simplified_master_multiplier": "Forza generale dei PID (cursore)",
    "dshot_bidir": "DShot bidirezionale (serve al filtro RPM)",
    "dshot_edt": "Telemetria estesa ESC (temperatura)",
    "dyn_notch_count": "Numero di filtri notch dinamici",
    "motor_poles": "Poli dei motori",
    "small_angle": "Angolo massimo per armare",
    "craft_name": "Nome del drone",
}


def nome_umano(param):
    return NOMI_PARAMETRI.get(param, param)


def consiglio(param, attuale, consigliato, titolo, perche, quanto, prova):
    return {"param": param, "attuale": str(attuale), "consigliato": str(consigliato), "titolo": titolo,
            "perche": perche, "quanto": quanto, "prova": prova}


# ============================================================== 1. configurazione

PREDEFINITI = {
    "dshot_bidir": "OFF", "dshot_edt": "OFF", "motor_poles": "14",
    "dyn_notch_count": "3", "gps_rescue_min_sats": "8",
    "gps_rescue_allow_arming_without_fix": "OFF", "gps_set_home_point_once": "OFF",
    "small_angle": "25", "failsafe_procedure": "DROP", "rssi_channel": "0", "failsafe_delay": "15",
    "osd_rssi_dbm_alarm": "-60", "osd_link_quality_pos": "0", "osd_rssi_dbm_pos": "0",
    "vbat_max_cell_voltage": "430", "vbat_warning_cell_voltage": "350", "vbat_min_cell_voltage": "330",
    "force_battery_cell_count": "0",
}

# KV dei motori: intervalli consigliati per misura di elica e numero di celle.
# Fonte: Oscar Liang, "Table of prop, motor, LiPo and weight" (oscarliang.com/table-prop-motor-lipo-weight),
# consultata l'08/11/2026. Si usano tutte le classi (freestyle, racing, long range, cinewhoop) della stessa misura.
TABELLA_KV = [
    # pollici, celle, KV min, KV max
    (2, 2, 10000, 15000), (2, 3, 6000, 8000), (2, 4, 5000, 6000),
    (2.5, 2, 7500, 10000), (2.5, 3, 6000, 8000), (2.5, 4, 4500, 6000),
    (3, 2, 4500, 8000), (3, 3, 4000, 6500), (3, 4, 3000, 5000), (3, 6, 2500, 3000),
    (3.5, 4, 2500, 4000), (3.5, 6, 1800, 3000),
    (4, 3, 3000, 4000), (4, 4, 2400, 3000), (4, 6, 1800, 2500),
    (5, 4, 2300, 3000), (5, 6, 1600, 2300),
    (6, 4, 2100, 2500), (6, 6, 1500, 1900),
    (7, 4, 1600, 1900), (7, 6, 980, 1400),
    (8, 6, 900, 1200),
    (10, 6, 900, 900),
]


# Joshua Bardwell, "The FPV Shopping List - 5 Inch Freestyle" (fpvknowitall.com, consultata l'08/11/2026):
# per il 5" freestyle 6S consiglia 1700-1950 KV (es. Xing2 1855, Xing-E 1800); le versioni 2400-2700 KV sono per la 4S.
BARDWELL_KV = {(5, 6): (1700, 1950), (5, 4): (2400, 2700)}


def kv_motori(comp):
    """KV dal campo apposito o, se manca, dal testo dei motori (es. '2407E 1750KV')."""
    m = re.search(r"(\d{3,5})", comp.get("kv_motori", "") or "")
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{3,5})\s*kv", comp.get("motori", "") or "", re.I)
    return int(m.group(1)) if m else None


def migliaia(x):
    """61740 -> '61.740' (punto delle migliaia all'italiana)."""
    return f"{x:,.0f}".replace(",", ".")


def giri_teorici(kv, celle, volt_cella=4.2):
    return kv * celle * volt_cella


def intervallo_kv(pollici, celle):
    """Intervallo di KV consigliato per quella misura e quelle celle. Se la tabella non ha proprio quelle celle,
    lo ricava dalla stessa misura con un'altra batteria, a parità di giri (KV × tensione)."""
    righe = [r for r in TABELLA_KV if abs(r[0] - pollici) <= 0.25]
    if not righe:
        return None, False
    esatta = [r for r in righe if r[1] == celle]
    if esatta:
        return (esatta[0][2], esatta[0][3]), True
    giri_min = min(r[2] * r[1] for r in righe)
    giri_max = max(r[3] * r[1] for r in righe)
    return (round(giri_min / celle, -1), round(giri_max / celle, -1)), False


# Profili delle batterie: tensioni per cella (volt).
# Fonti dei produttori (consultate il 07/11/2026):
#  - Tattu, manuale LiPo: carica massima 4,2 V; "mai sotto 3 V per cella sotto carico"; per i droni
#    "tensione di taglio consigliata 3,5 V per cella"; conservazione 3,6-3,9 V.
#  - Tattu, manuale serie Advanced (HV): carica massima 4,35 V, protezione da sovraccarica a 4,4 V.
#  - Ovonic, manuale: carica massima 4,2 V; mai sotto 3,0 V sotto carico; storage 3,7-3,85 V.
#  - Molicel P42A (cella Li-ion 21700), scheda tecnica: carica 4,2 V, taglio assoluto 2,5 V.
# Articoli FPV (consultati il 07/11/2026):
#  - Oscar Liang, guida LiPo: atterrare quando in volo si arriva a 3,5-3,6 V; 3,0 è il limite, non un
#    obiettivo; nelle accelerate un calo momentaneo a 3,3 V è normale; storage 3,80-3,85 V.
#  - Oscar Liang, LiHV: carica 4,35 V; minimo uguale alle LiPo; invecchiano più in fretta delle LiPo.
#  - Oscar Liang, Li-ion: carica 4,2 V; si può scendere a 3,0 V (lui atterra a 2,8); storage circa 3,8;
#    in Betaflight conviene fissare il numero di celle (force_battery_cell_count).
#  - CNHL, LiHV: storage 3,80-3,85; in volo non sotto 3,5; a riposo 3,6-3,7; vicino a 3,3 si rovinano.
#  - iFlight: LiPo in volo non sotto circa 3,5; a riposo sopra 3,7; storage 3,7-3,85.
#  - Joshua Bardwell (fpvknowitall.com, guida ai caricatori): storage 3,8 V per cella; carica a 1C la più sicura;
#    le LiPo "odiano" restare a lungo piene o vuote. Non dà una tensione di atterraggio.
# Regole dell'autore del progetto (istruttore di volo): LiPo e LiHV si atterra a 3,5 V per cella (coincide con Tattu);
# Li-ion al massimo a 3,2 V (lui di solito a 3,3): c'è chi arriva a 2,9, ma alla lunga si rovinano.
PROFILI_BATTERIA = {
    "LiPo": {"nome": "LiPo", "piena": 4.20, "anomala_sopra": 4.30, "carica_da": 4.15, "storage": 3.80,
             "scarica_sotto": 3.70, "impossibile_sotto": 3.0, "minimo_volo": 3.00,
             "atterraggio": 3.50, "bf_max": 430, "bf_avviso": 350, "bf_minimo": 330, "calo_forte": 3.30,
             "fonte_minimo": "Tattu e Ovonic: mai sotto 3,0 V per cella sotto carico",
             "fonte_atterraggio": "Tattu: tensione di taglio consigliata per i droni 3,5 V per cella"},
    "LiHV": {"nome": "LiHV", "piena": 4.35, "anomala_sopra": 4.40, "carica_da": 4.30,
             "storage": 3.85, "scarica_sotto": 3.70, "impossibile_sotto": 3.0, "minimo_volo": 3.00,
             "atterraggio": 3.50, "bf_max": 440, "bf_avviso": 350, "bf_minimo": 330, "calo_forte": 3.30,
             "fonte_minimo": "Tattu: mai sotto 3,0 V per cella sotto carico",
             "fonte_atterraggio": "Tattu: tensione di taglio consigliata per i droni 3,5 V per cella"},
    "Li-ion": {"nome": "Li-ion", "piena": 4.20, "anomala_sopra": 4.30, "carica_da": 4.10, "storage": 3.70,
               "scarica_sotto": 3.20, "impossibile_sotto": 2.5, "minimo_volo": 3.00,
               "atterraggio": 3.20, "bf_max": 430, "bf_avviso": 330, "bf_minimo": 320, "calo_forte": None,
               "fonte_minimo": "il limite assoluto delle celle è 2,5 V (Molicel), ma sotto 3,0 V si rovinano in fretta",
               "fonte_atterraggio": "regola pratica per le Li-ion: atterrare al massimo a 3,2 V per cella"},
}


def profilo_batteria(comp):
    """Il profilo della batteria scritto nei componenti; LiPo se non indicato."""
    t = comp.get("tipo_batteria", "").upper().replace(" ", "").replace("-", "")
    if "HV" in t:
        return PROFILI_BATTERIA["LiHV"]
    if "LION" in t or "LIION" in t or "18650" in t or "21700" in t:
        return PROFILI_BATTERIA["Li-ion"]
    return PROFILI_BATTERIA["LiPo"]


def volt(x):
    """Tensione scritta all'italiana: 3,30 V."""
    return f"{x:.2f}".replace(".", ",")

FUNZIONI = {
    1: "MSP", 2: "GPS", 4: "Telemetria FrSky", 32: "Telemetria SmartPort",
    64: "Ricevitore", 128: "Blackbox", 1024: "Sensore ESC", 2048: "VTX SmartAudio",
    8192: "VTX Tramp", 16384: "RunCam", 131072: "VTX MSP (DJI/HD)",
}


def nome_porta(n):
    if n < 20:
        return f"UART{n + 1}"
    if n == 20:
        return "USB"
    if 30 <= n < 40:
        return f"SOFTSERIAL{n - 29}"
    if 40 <= n < 50:
        return f"LPUART{n - 39}"
    return f"porta {n}"


def funzioni(maschera):
    return [nome for bit, nome in FUNZIONI.items() if maschera & bit] or ["nessuna"]


def leggi_configurazione(nome):
    print("Leggo la configurazione dal drone ...")
    with CLI() as cli:
        diff = cli.comando("diff all", attesa=20)
        porte = cli.comando("serial")
    print("(la scheda si sta riavviando, è normale)")
    momento = f"{datetime.now():%Y-%m-%d_%H-%M}"
    percorso = os.path.join(cartella(nome), f"diff_{momento}.txt")
    open(percorso, "w", encoding="utf-8").write("\n".join(diff) + "\n")
    open(os.path.join(cartella(nome), f"porte_{momento}.txt"), "w", encoding="utf-8").write("\n".join(porte) + "\n")
    tabella = {}
    for r in porte:
        p = r.split()
        if len(p) >= 3 and p[0] == "serial":
            tabella[int(p[1])] = int(p[2])
    return percorso, diff, tabella


def interpreta_diff(righe):
    dati = {"versione": "", "feature": set(), "imp": {}, "profili": 0, "rate": 0}
    sezione = None
    for riga in righe:
        r = riga.strip(); p = r.split()
        if r.startswith("# Betaflight"):
            dati["versione"] = r[2:]
        elif r.startswith("feature ") and len(p) > 1:
            (dati["feature"].discard(p[1][1:]) if p[1].startswith("-") else dati["feature"].add(p[1]))
        elif r.startswith("profile "):
            sezione = "profili" if p[1] == "0" else "altro"
        elif r.startswith("rateprofile "):
            sezione = "rate" if p[1] == "0" else "altro"
        elif r.startswith("set "):
            m = re.match(r"set\s+(\S+)\s*=\s*(.*)", r)
            if m:
                if sezione is None:
                    dati["imp"][m.group(1)] = m.group(2).strip()
                elif sezione in ("profili", "rate"):
                    dati[sezione] += 1
    return dati


def numero_porta(testo):
    m = re.match(r"\s*(UART|SOFTSERIAL|LPUART)\s*(\d+)", testo.upper())
    if not m:
        return None
    tipo, n = m.group(1), int(m.group(2))
    return {"UART": n - 1, "SOFTSERIAL": n + 29, "LPUART": n + 39}[tipo]


# Glossario: le parole tecniche spiegate in due righe. Nella finestra diventano cliccabili.
GLOSSARIO = {
    "filtro RPM": "Filtro che conosce i giri esatti di ogni motore e toglie proprio le vibrazioni che i motori "
                  "producono. Funziona solo se gli ESC comunicano i giri (DShot bidirezionale).",
    "DShot bidirezionale": "Modo di dialogo tra scheda ed ESC: la scheda manda i comandi e gli ESC rispondono con i "
                           "giri dei motori. Serve al filtro RPM.",
    "DShot": "Il linguaggio digitale con cui la scheda di volo comanda gli ESC.",
    "ESC": "Il regolatore dei motori: riceve i comandi dalla scheda e dà corrente ai motori. Spesso sono quattro in "
           "una sola scheda (4in1).",
    "OSD": "Le scritte sovrapposte all'immagine negli occhiali: tensione della batteria, tempo di volo, avvisi.",
    "failsafe": "Cosa fa il drone quando perde il segnale della radio: fermarsi e cadere, oppure tornare a casa con "
                "il GPS.",
    "GPS Rescue": "Il ritorno a casa automatico di Betaflight: se perdi il segnale, il drone usa il GPS per rientrare "
                  "verso il punto di decollo.",
    "fix": "Il momento in cui il GPS ha trovato abbastanza satelliti da sapere con precisione dove si trova.",
    "notch": "Un filtro che toglie una sola frequenza di vibrazione, come un tappo su un rumore preciso.",
    "PID": "Le tre regolazioni con cui il drone tiene l'assetto che gli chiedi con gli stick: P reagisce subito, I corregge gli "
           "errori che durano, D frena e smorza i rimbalzi.",
    "tuning": "La regolazione fine di PID e filtri per far volare il drone nel modo più preciso e pulito.",
    "Bluejay": "Un firmware moderno e gratuito per ESC di tipo BLHeli_S: abilita il DShot bidirezionale.",
    "BLHeli_S": "Un tipo diffuso di ESC economici a 8 bit. Con il firmware Bluejay diventano più capaci.",
    "telemetria": "Dati che un componente rimanda alla scheda: per esempio gli ESC che comunicano giri o temperatura.",
    "turtle": "Funzione per raddrizzare il drone ribaltato: fa girare al contrario due motori per rimetterlo dritto.",
    "ARM": "Armare significa abilitare i motori: da quel momento il drone può girare le eliche. Disarmare li blocca.",
    "armare": "Abilitare i motori: da quel momento il drone può girare le eliche.",
    "UART": "Una porta di comunicazione della scheda di volo: a ciascuna si collega un componente (ricevente, GPS, "
            "video).",
    "Serial RX": "L'impostazione che dice alla scheda: su questa porta c'è la ricevente della radio.",
    "ricevente": "Il piccolo ricevitore sul drone che riceve i comandi della radio.",
    "ELRS": "ExpressLRS: sistema radio moderno e a lungo raggio, molto diffuso. Usa il protocollo CRSF.",
    "Crossfire": "Sistema radio a lungo raggio di TBS. Usa il protocollo CRSF, come ELRS.",
    "CRSF": "Il linguaggio con cui le riceventi ELRS e Crossfire parlano con la scheda di volo.",
    "blackbox": "La scatola nera del drone: registra i dati del volo nella memoria della scheda.",
    "KV": "Quanti giri al minuto fa un motore per ogni volt, senza eliche. Più celle = più volt = più giri: per "
          "questo con la 6S si usano motori con KV più basso che con la 4S.",
    "poli": "I magneti nella campana del motore. Servono al filtro RPM per calcolare le vibrazioni; quasi sempre 14.",
    "propwash": "Le vibrazioni che si sentono scendendo dentro l'aria smossa dalle proprie eliche.",
    "feedforward": "Una spinta in anticipo che fa reagire il drone appena muovi lo stick, prima ancora che si accorga "
                   "dell'errore.",
    "Betaflight": "Il programma che gira sulla scheda di volo e fa volare il drone. Si configura con Betaflight "
                  "Configurator.",
    "VTX": "Il trasmettitore video: manda l'immagine della telecamera agli occhiali.",
    "4in1": "Quattro ESC riuniti su una sola scheda, uno per motore.",
    "LiPo": "La batteria più diffusa per i droni FPV: piena a 4,20 V per cella, storage a circa 3,80 V. Si "
            "atterra a 3,5 V per cella. La carica più sicura è a 1C (circa un'ora).",
    "LiHV": "LiPo ad alta tensione: piena a 4,35 V per cella. Un po' più di spinta a inizio volo; va caricata "
            "con il programma HV del caricatore. Invecchia un po' più in fretta di una LiPo normale.",
    "Li-ion": "Batterie con celle tipo 18650 o 21700: meno spinta ma molta più autonomia. C'è chi le scarica fino "
              "a 2,9 V per cella, ma alla lunga si rovinano: si atterra al massimo a 3,2 V per cella.",
    "storage": "La tensione giusta per conservare una batteria per giorni o settimane senza rovinarla.",
    "stack": "La scheda di volo e la scheda degli ESC impilate una sull'altra.",
}


def capito(significa, fare=None):
    """Il cuore del linguaggio di FPV Doctor: dopo 'cosa ho visto' (il titolo) vengono sempre
    'cosa significa' e, quando serve, 'cosa fare'."""
    testo = f"Cosa significa: {significa}"
    if fare:
        testo += f"\nCosa fare: {fare}"
    return testo


def rientra(testo, margine="    "):
    return "\n".join(margine + riga for riga in str(testo).split("\n"))


VIDEO_DIGITALE = ("DJI", "O4", "O3", "WALKSNAIL", "AVATAR", "HDZERO", "DIGITALE", "VISTA")


def digitale_osd_attivo(porte):
    """True se una porta manda l'OSD al video digitale (VTX MSP / DisplayPort)."""
    return any(m & 131072 for m in porte.values())


def controlla(dati, porte, comp, rilevati_ciclo=None):
    rap = []
    voluti = {v.strip().lower() for v in comp.get("voluto", "").split(",") if v.strip()}

    def val(k):
        return dati["imp"].get(k, PREDEFINITI.get(k, "")).upper()

    def add(livello, visto, significa, fare=None, chiave=None, rimedio=None):
        if chiave and chiave in voluti and livello in ("ATTENZIONE", "VERIFICA"):
            livello = "NOTA"
            fare = (fare + " " if fare else "") + "(L'hai segnata come scelta voluta.)"
        rap.append((livello, visto, capito(significa, fare), rimedio))

    # --- ricevente
    rx = [n for n, m in porte.items() if m & 64]
    attesa_rx = numero_porta(comp.get("porta_ricevitore", ""))
    if not rx:
        add("ATTENZIONE", "Nessuna porta è impostata per la ricevente",
            "Il drone non sa da dove arrivano i comandi della radio: non risponderebbe agli stick.",
            "In Betaflight Configurator, scheda Porte, attiva «Serial RX» sulla porta dove è saldata la ricevente.")
    elif attesa_rx is not None and attesa_rx not in rx:
        fc = ", ".join(nome_porta(n) for n in rx)
        add("ATTENZIONE", f"La ricevente risulta su {fc} invece che su {nome_porta(attesa_rx)}",
            f"Nei componenti hai indicato {nome_porta(attesa_rx)}, ma la scheda la aspetta su {fc}. Se la ricevente "
            f"è davvero saldata su {nome_porta(attesa_rx)}, il drone non riceve i comandi.",
            "Controlla su quale porta è saldata: se ha ragione la scheda correggi i componenti, altrimenti "
            "cambia la porta in Betaflight (scheda Porte).")
    else:
        add("OK", f"Ricevente impostata su {', '.join(nome_porta(n) for n in rx)}",
            "La scheda sa da dove arrivano i comandi della radio.")

    # Regole dalle guide di Oscar Liang (ExpressLRS, failsafe, LQ e RSSI), consultate l'08/11/2026
    ril = rilevati_ciclo or {}
    protocollo_rx = ril.get("protocollo")
    if protocollo_rx and confronta_radio(comp.get("ricevitore", ""), protocollo_rx) is False:
        add("ATTENZIONE", f"La scheda usa il protocollo {protocollo_rx}, che non è quello della tua radio",
            f"Nei componenti hai scritto «{comp.get('ricevitore')}», ma la scheda aspetta {protocollo_rx}: i comandi "
            "della radio non verrebbero capiti.",
            "In Betaflight Configurator, scheda Ricevitore, scegli il protocollo giusto (CRSF per ELRS e "
            "Crossfire), salva e ripeti il test della ricevente.")
    soft_rx = [n for n in rx if 30 <= n < 40]
    if soft_rx:
        add("ATTENZIONE", f"La ricevente è su {nome_porta(soft_rx[0])}, una porta simulata (soft serial)",
            "Le porte soft serial sono porte «finte» create dal processore: troppo lente per la ricevente "
            "(Oscar Liang: «non usare il soft serial per la ricevente»). I comandi arriverebbero in ritardo o a scatti.",
            "Collega la ricevente a una UART vera e libera e attiva lì «Serial RX».")
    crsf = protocollo_rx == "CRSF" or "elrs" in comp.get("ricevitore", "").lower() or \
        "crossfire" in comp.get("ricevitore", "").lower()
    if crsf and rx and "TELEMETRY" not in dati["feature"]:
        add("NOTA", "La telemetria verso la radio è spenta",
            "Con ELRS e Crossfire la scheda può mandare alla radio tensione della batteria, qualità del segnale e "
            "GPS. Spenta, la radio non può avvisarti con la voce quando la batteria è bassa (Oscar Liang consiglia "
            "di attivarla).",
            "Accendere la telemetria. Posso farlo io da Correggi e migliora.",
            "feature_telemetry", ("feature_telemetry", "ON"))
    if crsf and str(val("rssi_channel") or "0") not in ("0", ""):
        add("NOTA", f"Il segnale (RSSI) è letto dal canale {val('rssi_channel')}",
            "Con ELRS e Crossfire la qualità del segnale arriva già dentro il protocollo: leggerla da un canale può "
            "dare valori sbagliati sull'OSD (Oscar Liang: «RSSI Channel su Disabled»).",
            "Riportare il canale RSSI a «disabilitato». Posso farlo io da Correggi e migliora.",
            "rssi_channel", ("rssi_channel", "0"))
    if crsf and digitale_osd_attivo(porte):
        visibile = lambda k: (int(val(k) or 0) & 0x0800) != 0
        if not visibile("osd_link_quality_pos"):
            add("NOTA", "La qualità del segnale (LQ) non si vede negli occhiali",
                "È l'indicatore più importante per capire quando tornare indietro: Oscar Liang consiglia di tornare "
                "se scende sotto il 50%. Senza, ti accorgi di essere troppo lontano solo quando l'immagine o i "
                "comandi iniziano a perdersi.",
                "In Betaflight Configurator, scheda OSD, attiva «Link Quality» (e se vuoi anche «RSSI dBm»).")
        if visibile("osd_rssi_dbm_pos") and int(val("osd_rssi_dbm_alarm") or -60) > -80:
            add("NOTA", f"L'allarme del segnale (RSSI dBm) è a {val('osd_rssi_dbm_alarm') or -60} dBm: scatta troppo presto",
                "Con questo valore l'avviso compare quasi sempre, anche quando il segnale è buono, e finisci per "
                "ignorarlo (Oscar Liang lo definisce «troppo alto»). Lui lo mette circa 10 dB sopra il limite del "
                "pacchetto: -95 dBm a 500 Hz, -98 a 250 Hz.",
                "Portare l'allarme a -95 dBm. Posso farlo io da Correggi e migliora.",
                "osd_rssi_dbm_alarm", ("osd_rssi_dbm_alarm", "-95"))
    ritardo_fs = int(val("failsafe_delay") or 15)
    if ritardo_fs > 20:
        add("VERIFICA", f"Il failsafe parte dopo {ritardo_fs / 10:.1f} secondi".replace(".", ","),
            "Per tutto quel tempo, senza segnale, il drone continua con gli stick al centro e il gas al minimo. "
            "Betaflight parte da 1,5 secondi e Oscar Liang consiglia di non cambiarlo se non sai bene perché.",
            "Riportarlo a 1,5 secondi (valore 15). Posso farlo io da Correggi e migliora.",
            "failsafe_delay", ("failsafe_delay", "15"))

    for testo in comp.get("porte_da_evitare", "").split(","):
        n = numero_porta(testo)
        if n is not None and porte.get(n, 0):
            add("ATTENZIONE", f"{nome_porta(n)} è in uso, ma l'avevi segnata come porta da non usare",
                f"Su {nome_porta(n)} è attivo: {', '.join(funzioni(porte[n]))}. Se quella porta ha un problema "
                "(piazzola rovinata, disturbi), quel collegamento può non funzionare in volo.",
                f"Sposta il collegamento su una porta libera, oppure togli {nome_porta(n)} dalle porte da non usare "
                "se ora funziona bene.")

    # --- video digitale
    if any(v in comp.get("vtx", "").upper() for v in VIDEO_DIGITALE):
        if any(m & 131072 for m in porte.values()):
            add("OK", "Le scritte a schermo arrivano agli occhiali",
                "La porta del video digitale è impostata per l'OSD: tensione della batteria, tempo di volo e avvisi.")
        else:
            add("VERIFICA", "Le scritte a schermo potrebbero non arrivare agli occhiali",
                "Nessuna porta è impostata per l'OSD del video digitale: negli occhiali non vedresti tensione della "
                "batteria, avvisi e tempo di volo.",
                "In Betaflight, scheda Porte, sulla porta del video attiva «VTX (MSP + DisplayPort)».")

    # --- motori e filtri
    bidir = val("dshot_bidir") == "ON"
    if bidir:
        add("OK", "Lettura dei giri motore attiva",
            "Gli ESC dicono alla scheda quanto girano i motori: così il filtro RPM toglie con precisione le "
            "vibrazioni dei motori. È il filtro più efficace che esista.")
    else:
        add("ATTENZIONE", "Lettura dei giri motore spenta",
            "La scheda non sa quanto girano i motori, quindi il filtro RPM, il più efficace contro le vibrazioni, "
            "non lavora: il drone è più rumoroso e i motori scaldano di più.",
            "Si attiva con il DShot bidirezionale, che funziona con ESC Bluejay, BLHeli_32 o AM32. "
            "Posso attivarlo io da Correggi e migliora.", "dshot_bidir", ("dshot_bidir", "ON"))
    notch = int(val("dyn_notch_count") or 3)
    # Guida ufficiale Betaflight (4.3 Tuning Notes): con il filtro RPM di solito 1-2 notch dinamici,
    # senza filtro RPM 3-4.
    if not bidir and notch < 3:
        add("VERIFICA", f"Solo {notch} filtri notch dinamici senza il filtro RPM",
            "Senza il filtro RPM sono i notch dinamici a togliere le vibrazioni dei motori: la guida ufficiale di "
            "Betaflight ne consiglia 3-4.",
            "Portarli a 3, oppure (meglio) attivare il filtro RPM.", None, ("dyn_notch_count", "3"))
    if bidir and notch > 2:
        add("NOTA", f"{notch} filtri notch dinamici: probabilmente troppi",
            "Con il filtro RPM attivo ne basta quasi sempre uno (guida ufficiale di Betaflight: di solito 1-2). Ogni "
            "filtro in più aggiunge un piccolo ritardo nella risposta ai comandi.",
            "Portarli a 1, poi controllare il rumore con un volo e Analizza un volo.", None, ("dyn_notch_count", "1"))
    elif bidir:
        add("OK", "Filtri notch dinamici nel numero giusto",
            "Con il filtro RPM attivo ne bastano pochi: il drone resta pronto ai comandi.")
    poli = comp.get("poli_motore")
    if poli and poli != val("motor_poles"):
        add("ATTENZIONE", f"Poli dei motori: la scheda ne usa {val('motor_poles')}, i tuoi motori ne hanno {poli}",
            "Il filtro RPM calcola le vibrazioni dai giri e dal numero di poli: con il numero sbagliato filtra le "
            "frequenze sbagliate e lascia passare le vibrazioni vere.",
            "Conta i magneti nella campana del motore (quasi sempre 14). Se nei componenti è giusto, posso "
            "correggere io la scheda da Correggi e migliora.", "motor_poles", ("motor_poles", poli))

    # --- motori: KV adatto a eliche e batteria?
    kv = kv_motori(comp)
    celle_m = comp.get("celle_batteria", "")
    pollici_m = pollici_eliche(comp.get("eliche", ""))
    if not kv:
        add("NOTA", "Non conosco il KV dei motori",
            "Il KV dice quanti giri fa il motore per ogni volt. Senza, non posso controllare se motori, eliche e "
            "batteria vanno d'accordo: un motore nato per la 4S su una 6S gira molto più veloce e rischia di "
            "surriscaldarsi.",
            "Scrivi il KV nei Componenti (lo trovi sul motore o sulla sua scheda tecnica).")
    elif celle_m.isdigit() and pollici_m:
        celle_m = int(celle_m)
        rng, esatto = intervallo_kv(pollici_m, celle_m)
        if rng:
            lo, hi = rng
            giri = giri_teorici(kv, celle_m)
            fonte = "tabella di Oscar Liang" + ("" if esatto else ", ricavata dalla stessa misura con un'altra batteria")
            jb = BARDWELL_KV.get((round(pollici_m), celle_m)) if abs(pollici_m - round(pollici_m)) < 0.25 else None
            if jb:
                fonte += f"; Joshua Bardwell per il freestyle consiglia {jb[0]}-{jb[1]} KV"
            eq = [r for r in TABELLA_KV if abs(r[0] - pollici_m) <= 0.25 and r[1] < celle_m and r[2] <= kv <= r[3]]
            if hi < kv <= hi * 1.05:
                add("VERIFICA", f"Motori da {kv} KV al limite alto per una {celle_m}S",
                    f"Per questa misura con {celle_m} celle si usano motori da {lo:.0f} a {hi:.0f} KV ({fonte}): i tuoi "
                    "sono appena sopra. Di solito va bene, ma i motori lavorano più caldi.",
                    "Dopo i primi voli con la batteria piena tocca i motori: devono essere tiepidi, non caldi.")
            elif kv > hi:
                nato = f" È un KV pensato per la {eq[-1][1]}S." if eq else ""
                add("ATTENZIONE", f"Motori da {kv} KV troppo veloci per una {celle_m}S con eliche da "
                    f"{str(pollici_m).rstrip('0').rstrip('.').replace('.', ',')} pollici",
                    f"Per questa misura con {celle_m} celle si usano motori da {lo:.0f} a {hi:.0f} KV ({fonte}).{nato} "
                    f"A batteria piena girerebbero fino a circa {migliaia(giri)} giri al minuto senza carico: più "
                    "corrente, più calore, rischio di bruciare motori o ESC in volo.",
                    f"Usa una batteria con meno celle o motori da {lo:.0f}-{hi:.0f} KV. Se il KV scritto è sbagliato, "
                    "correggilo nei Componenti. Dopo i primi voli tocca i motori: devono essere tiepidi.")
            elif kv < lo * 0.85:
                add("NOTA", f"Motori da {kv} KV un po' lenti per una {celle_m}S con eliche da "
                    f"{str(pollici_m).rstrip('0').rstrip('.').replace('.', ',')} pollici",
                    f"Di solito si usano motori da {lo:.0f} a {hi:.0f} KV ({fonte}): il drone potrebbe avere poca "
                    "spinta e reagire con meno grinta. Va bene se è una scelta per l'autonomia (long range).",
                    "Controlla nell'analisi del volo se i motori arrivano spesso al massimo.")
            else:
                add("OK", f"Motori da {kv} KV adatti a una {celle_m}S con eliche da "
                    f"{str(pollici_m).rstrip('0').rstrip('.').replace('.', ',')} pollici",
                    f"Per questa misura si usano motori da {lo:.0f} a {hi:.0f} KV ({fonte}).")

    # --- tensione massima di ESC e scheda di volo (Oscar Liang, guida agli ESC: una tensione troppo alta
    #     per l'ESC lo danneggia; la maggior parte arriva alla 6S, alcuni solo alla 4S)
    celle_b = comp.get("celle_batteria", "")
    for chiave_c, nome_c in (("esc_celle_max", "gli ESC"), ("fc_celle_max", "la scheda di volo")):
        mx = comp.get(chiave_c, "")
        if celle_b.isdigit() and mx.isdigit() and int(celle_b) > int(mx):
            add("ATTENZIONE", f"Batteria {celle_b}S, ma {nome_c} reggono al massimo {mx}S",
                f"Una tensione più alta di quella per cui sono costruiti brucia {nome_c} appena attacchi la "
                "batteria, anche a terra e senza volare (Oscar Liang, guida agli ESC).",
                f"NON attaccare una {celle_b}S. Usa batterie fino a {mx}S, oppure controlla sulla scheda tecnica "
                "se il valore nei Componenti è giusto.")
    if celle_b.isdigit() and not comp.get("esc_celle_max") and int(celle_b) >= 5:
        add("NOTA", "Non so fino a quante celle reggono gli ESC",
            f"Usi una {celle_b}S: la maggior parte degli ESC di oggi arriva alla 6S, ma alcuni solo alla 4S, e una "
            "tensione troppo alta li brucia.",
            "Scrivi nei Componenti le celle massime di ESC e scheda di volo (sono sulla scheda tecnica, es. 3-6S).")

    # --- protocollo dei motori e velocità del ciclo (Oscar Liang: DShot150 fino a 2 kHz, DShot300 fino a
    #     4 kHz, DShot600 per 8 kHz)
    protocollo = val("motor_pwm_protocol").upper()
    ciclo = rilevati_ciclo.get("ciclo_us") if rilevati_ciclo else None
    if ciclo and protocollo.startswith("DSHOT"):
        khz = 1000 / ciclo
        limite = {"DSHOT150": 2.1, "DSHOT300": 4.2, "DSHOT600": 8.5}.get(protocollo)
        if limite and khz > limite:
            consigliato = "DSHOT300" if khz <= 4.2 else "DSHOT600"
            add("VERIFICA", f"{protocollo.replace('DSHOT', 'DShot')} è lento per un ciclo a {khz:.1f} kHz".replace(".", ","),
                f"La scheda calcola i PID {khz * 1000:.0f} volte al secondo, ma con questo protocollo non riesce a "
                "mandare i comandi agli ESC così spesso (Oscar Liang: DShot300 fino a 4 kHz, DShot600 per 8 kHz). "
                "Con il DShot bidirezionale può dare errori di comunicazione.",
                f"Portare il protocollo a {consigliato.replace('DSHOT', 'DShot')}. Posso farlo io da Correggi e migliora.",
                "motor_pwm_protocol", ("motor_pwm_protocol", consigliato))

    # --- ESC
    fw = comp.get("firmware_esc", "").lower()
    if "blheli_s" in fw.replace("-", "_") and not bidir:
        add("NOTA", "ESC con BLHeli_S: con Bluejay avresti il filtro RPM",
            "Il BLHeli_S non riceve più aggiornamenti e non manda i giri dei motori alla scheda. Bluejay è il "
            "firmware gratuito per gli stessi ESC che lo fa (Oscar Liang lo consiglia).",
            "Aggiorna gli ESC a Bluejay con il configuratore online (esc-configurator.com), poi attiva il DShot "
            "bidirezionale.")
    if "ESC_SENSOR" in dati["feature"] and ("bluejay" in fw or "blheli_s" in fw):
        add("NOTA", "La lettura del filo di telemetria degli ESC è attiva, ma non serve",
            "Gli ESC Bluejay e BLHeli_S non hanno quel filo: la funzione non riceve nulla. Non fa danni.",
            "Puoi lasciarla così.")

    if "CHANNEL_FORWARDING" in dati["feature"]:
        add("NOTA", "È attiva la funzione Channel forwarding, che su un drone di solito non serve",
            "Serve a comandare servi o gimbal dai canali AUX. Su un quadricottero senza servi non fa nulla: spesso "
            "resta attiva da una configurazione di fabbrica o copiata da un altro drone. Non fa danni.",
            "Se non comandi servi o gimbal, puoi spegnerla per avere una configurazione più pulita. Posso farlo io "
            "da Correggi e migliora.", "feature_channel_forwarding", ("feature_channel_forwarding", "OFF"))

    # --- GPS e perdita del segnale
    gps = "GPS" in dati["feature"]
    procedura = val("failsafe_procedure")
    if procedura == "GPS-RESCUE":
        if gps:
            add("OK", "Se perdi il segnale, il drone torna a casa",
                "È impostato il GPS Rescue: se la radio si scollega, il drone usa il GPS per rientrare al punto di decollo.")
        else:
            add("ATTENZIONE", "Il ritorno a casa è impostato, ma il GPS è spento",
                "Se perdi il segnale il drone non può tornare: senza GPS cadrebbe.",
                "Attiva il GPS in Betaflight (scheda Configurazione) oppure scegli un'altra procedura di failsafe.")
    elif not gps:
        add("NOTA", "Se perdi il segnale, i motori si fermano e il drone scende",
            "È il comportamento normale per un drone senza GPS: dopo un breve tentativo di attesa, il failsafe "
            "spegne i motori.",
            "Vola entro la portata sicura della radio e lontano da persone e cose.")
    if gps:
        if any(m & 2 for m in porte.values()):
            add("OK", "GPS collegato a una porta", "La scheda sa su quale porta ricevere i dati del GPS.")
        else:
            add("ATTENZIONE", "GPS attivo ma senza porta",
                "La funzione GPS è accesa, ma nessuna porta è impostata per il GPS: la scheda non riceve la posizione.",
                "In Betaflight, scheda Porte, attiva «GPS» sulla porta dove è saldato il modulo.")
        if val("gps_rescue_allow_arming_without_fix") == "ON":
            add("ATTENZIONE", "Il drone si può armare prima che il GPS trovi i satelliti",
                "Se decolli prima del fix il punto di casa non viene salvato: perdendo il segnale il ritorno a casa "
                "non funzionerebbe e il drone cadrebbe.",
                "Prima di decollare aspetta che l'OSD mostri il fix e la freccia di casa.",
                "gps_rescue_allow_arming_without_fix")
        sats = comp.get("satelliti_tipici", "")
        minimo = int(val("gps_rescue_min_sats") or 8)
        if sats.isdigit() and int(sats) < minimo:
            add("VERIFICA", f"Di solito vedi {sats} satelliti, il ritorno a casa ne chiede {minimo}",
                f"Con meno di {minimo} satelliti la scheda non considera il punto di casa abbastanza preciso per tornare.",
                "Aspetta più satelliti prima di decollare; dopo l'arm controlla sull'OSD la freccia e la distanza da casa.",
                "gps_rescue_min_sats")
        if val("gps_set_home_point_once") == "ON":
            add("NOTA", "Il punto di casa resta quello del primo decollo",
                "Se ti sposti e ridecolli senza staccare la batteria, il drone tornerebbe al primo punto.",
                "Se cambi posto di decollo, stacca e riattacca la batteria.")

    # --- batteria: le soglie di Betaflight devono corrispondere al tipo di batteria
    prof = profilo_batteria(comp)
    bf_max, bf_avv, bf_min = (int(val(k) or d) for k, d in (("vbat_max_cell_voltage", 430),
                                                            ("vbat_warning_cell_voltage", 350),
                                                            ("vbat_min_cell_voltage", 330)))
    if prof["nome"].startswith("LiHV") and bf_max < 435:
        add("ATTENZIONE", "Con le batterie HV la scheda può contare male le celle",
            f"La scheda considera piena una cella a {volt(bf_max / 100)} V, ma una HV carica arriva a 4,35: può "
            "contare una cella in più e sbagliare tutti gli avvisi di tensione.",
            "Alzare la tensione massima per cella a 4,40 V. Posso farlo io da Correggi e migliora.",
            "vbat_max_cell_voltage", ("vbat_max_cell_voltage", str(prof["bf_max"])))
    celle_c = comp.get("celle_batteria", "")
    if prof["nome"] == "Li-ion" and celle_c.isdigit() and val("force_battery_cell_count") in ("0", ""):
        add("VERIFICA", "Con le Li-ion la scheda può sbagliare il numero di celle",
            "La scheda conta le celle quando attacchi la batteria. Una Li-ion non carica del tutto ha una tensione "
            "più bassa e può essere scambiata per una batteria con una cella in meno: tensione per cella e avvisi "
            "sull'OSD diventano sbagliati.",
            f"Fissare il numero di celle a {celle_c} in Betaflight. Posso farlo io da Correggi e migliora.",
            "force_battery_cell_count", ("force_battery_cell_count", celle_c))
    if bf_avv < round(prof["atterraggio"] * 100):
        add("ATTENZIONE", f"L'avviso di batteria bassa scatta solo a {volt(bf_avv / 100)} V per cella",
            f"Per una {prof['nome']} si atterra a {volt(prof['atterraggio'])} V per cella: quando arriva l'avviso "
            "sei già oltre, e alla lunga le celle si rovinano.",
            f"Riportare l'avviso a {volt(prof['bf_avviso'] / 100)} V per cella. Posso farlo io da Correggi e migliora.",
            "vbat_warning_cell_voltage", ("vbat_warning_cell_voltage", str(prof["bf_avviso"])))

    if int(val("small_angle") or 25) >= 180:
        add("NOTA", "Il drone si può armare anche capovolto",
            "Serve per il turtle (raddrizzare il drone dopo un ribaltamento), ma aumenta il rischio di armarlo per "
            "sbaglio mentre lo tieni in mano.",
            "Quando maneggi il drone acceso, tieni le dita lontane dall'interruttore ARM.", "small_angle")
    if dati["profili"] == 0:
        eliche = comp.get("eliche", "")
        diverso = bool(re.search(r"\d", eliche)) and not re.search(r"\b5\b|5\"|5 ?poll|5\.1", eliche)
        add("NOTA", "I PID sono quelli di fabbrica",
            "Sono studiati per un 5 pollici medio: volano bene, ma un tuning su misura rende il drone più preciso." +
            (" Il tuo drone non è un 5 pollici, quindi c'è più margine di miglioramento." if diverso else ""),
            "Fai un volo di taratura con manovre decise, poi Analizza un volo: l'app ti dirà se e cosa cambiare.")
    return rap


def stampa_rapporto(rap):
    gruppi = [("ATTENZIONE", "⚠️  DA CONTROLLARE"), ("VERIFICA", "🔍 DA VERIFICARE"),
              ("NOTA", "ℹ️  NOTE"), ("OK", "✅ TUTTO A POSTO")]
    for livello, titolo in gruppi:
        voci = [v for v in rap if v[0] == livello]
        if voci:
            print(f"\n{titolo}")
            for _, t, perche, rimedio in voci:
                print(f"  • {t}\n{rientra(perche)}")
                if rimedio:
                    print(f"    Posso correggerlo io: {nome_umano(rimedio[0])} a {rimedio[1]} (pulsante Correggi e migliora).")


def menu_configurazione(drone):
    percorso, diff, porte = leggi_configurazione(drone["nome"])
    dati = interpreta_diff(diff)
    comp = leggi_componenti(drone["nome"])
    print(f"\nSalvata in: {percorso}")
    print("\nPorte seriali (tabella completa):")
    for n, m in sorted(porte.items()):
        if m:
            print(f"  {nome_porta(n):<10} {', '.join(funzioni(m))}")
    rap = controlla(dati, porte, comp, leggi_rilevati(drone))
    stampa_rapporto(rap)
    nuovi = []
    for livello, titolo, perche, rimedio in rap:
        if rimedio:
            tipo, prova = classe_sicurezza(rimedio[0])
            if rimedio[0].startswith("feature_"):
                attuale_r = "ON" if rimedio[0][8:].upper() in dati["feature"] else "OFF"
            else:
                attuale_r = dati["imp"].get(rimedio[0], PREDEFINITI.get(rimedio[0], "?"))
            nuovi.append(consiglio(rimedio[0], attuale_r,
                                   rimedio[1], titolo, perche, "correzione di configurazione",
                                   [prova] if prova else ["Nessuna prova di volo particolare"]))
    salva_consigli(drone, "configurazione", nuovi)
    salva_rapporto(drone, "configurazione", rap)
    if nuovi:
        print(f"\n{len(nuovi)} correzione/i consigliata/e: le trovi in 'Correggi e migliora'.")
    return rap


# ============================================================== 2. blackbox: scarica e cancella

def stato_flash(ser, attendi_pronta=True):
    """Stato della memoria blackbox. Dopo un riavvio la scheda impiega qualche secondo a
    scorrere la memoria: finché non è pronta, lo spazio occupato risulta 0 (falso).
    Per questo aspetto che sia pronta prima di fidarmi del numero."""
    scadenza = time.time() + 20
    avvisato = False
    while True:
        r = msp(ser, MSP_DATAFLASH_SUMMARY)
        if not r or len(r) < 13:
            return None
        flag, _, totale, usato = struct.unpack("<BIII", r[:13])
        stato = {"pronta": bool(flag & 1), "supportata": bool(flag & 2), "totale": totale, "usato": usato}
        if stato["pronta"] or not attendi_pronta or not totale:
            return stato
        if time.time() > scadenza:
            raise IOError("La memoria del blackbox non risulta pronta: riprova tra un minuto.")
        if not avvisato:
            print("La memoria del blackbox si sta ancora preparando, attendo ...")
            avvisato = True
        time.sleep(1)


def log_scaricati(drone):
    """Tutti i file blackbox di questo drone salvati sul Mac (cartella del drone e cartella principale)."""
    trovati = [os.path.join(cartella(drone["nome"]), f) for f in os.listdir(cartella(drone["nome"]))
               if f.endswith(".bbl")]
    trovati += [f for f in file_del_drone_fuori_posto(drone) if f.endswith(".bbl")]   # solo della sua scheda
    return sorted(trovati, key=os.path.getmtime)


def menu_scarica(drone):
    with apri() as ser:
        st = stato_flash(ser)
        if not st or not st["totale"]:
            print("Questo drone non ha una memoria flash per il blackbox (forse usa una scheda SD).")
            return
        print(f"Memoria blackbox: {st['usato'] / 1048576:.1f} MB occupati su {st['totale'] / 1048576:.1f} MB")
        if st["usato"] == 0:
            print("È vuota: niente da scaricare.")
            return
        nome_file = os.path.join(cartella(drone["nome"]), f"blackbox_{datetime.now():%Y-%m-%d_%H-%M}.bbl")
        usato, ind, inizio = st["usato"], 0, time.time()
        with open(nome_file, "wb") as f:
            while ind < usato:
                for tentativo in range(5):
                    try:
                        r = msp(ser, MSP_DATAFLASH_READ, struct.pack("<IHB", ind, 4096, 0))
                        a, n, comp = struct.unpack("<IHB", r[:7])
                        dati = r[7:7 + n]
                        if a == ind and comp == 0 and len(dati) == n:
                            break
                    except (TimeoutError, IOError):
                        pass
                    ser.reset_input_buffer(); time.sleep(0.2)
                else:
                    raise IOError("Lettura della memoria non riuscita.")
                if not dati:
                    break
                f.write(dati[:usato - ind]); ind += len(dati)
                perc = min(ind / usato * 100, 100)
                vel = ind / max(time.time() - inizio, 0.01) / 1024
                print(f"\r  [{'#' * int(perc / 4):<25}] {perc:5.1f}%  mancano ~{(usato - ind) / 1024 / max(vel, 1):4.0f} s ",
                      end="", flush=True)
    print(f"\n\nScaricato in: {nome_file}")
    print("Sul drone non è stato cancellato nulla.")


def menu_cancella(drone):
    with apri() as ser:
        st = stato_flash(ser)
        if not st or not st["totale"]:
            print("Questo drone non ha una memoria flash per il blackbox.")
            return
        print(f"Memoria blackbox: {st['usato'] / 1048576:.1f} MB occupati.")
        if st["usato"] == 0:
            print("La memoria è già vuota: non serve cancellare.")
            return
        scaricati = log_scaricati(drone)
        if scaricati:
            ultimo = scaricati[-1]
            print(f"File blackbox già salvati sul Mac: {len(scaricati)} (ultimo: {ultimo}, "
                  f"{os.path.getsize(ultimo) / 1048576:.1f} MB)")
            if os.path.getsize(ultimo) < st["usato"]:
                print("ATTENZIONE: sul drone ci sono più dati di quelli nell'ultimo file salvato.")
                print("Ti consiglio di scaricarli prima ('Scarica il blackbox').")
        else:
            print("ATTENZIONE: non ho trovato nessun file blackbox salvato sul Mac.")
            print("Ti consiglio di scaricarli prima ('Scarica il blackbox').")
        print("La cancellazione è definitiva.")
        if chiedi("Per confermare scrivi CANCELLA: ") != "CANCELLA":
            print("Annullato, non ho toccato nulla.")
            return
        msp(ser, MSP_DATAFLASH_ERASE)
        print("Cancellazione in corso (può richiedere fino a un paio di minuti) ...")
        inizio = time.time()
        time.sleep(1)
        while time.time() - inizio < 240:
            try:
                st = stato_flash(ser, attendi_pronta=False)
                if st and st["pronta"] and st["usato"] == 0:
                    print(f"\nFatto: memoria vuota in {time.time() - inizio:.0f} secondi.")
                    return
            except (TimeoutError, IOError):
                pass
            print(".", end="", flush=True)
            time.sleep(2)
    print("\nLa cancellazione sta durando molto: ricontrolla con 'Cancella i log dal drone' tra un minuto.")


# ============================================================== 3. analisi del blackbox

ENC = dict(SVB=0, UVB=1, NEG14=3, TAG8_8SVB=6, TAG2_3S32=7, TAG8_4S16=8, NULL=9, TAG2_3SVAR=10)


def sext(v, bits):
    m = 1 << (bits - 1)
    return (v & (m - 1)) - (v & m)


class Flusso:
    def __init__(self, d, pos, fine):
        self.d, self.p, self.fine = d, pos, fine

    def byte(self):
        if self.p >= self.fine:
            raise EOFError
        b = self.d[self.p]; self.p += 1
        return b

    def uvb(self):
        r = s = 0
        for _ in range(5):
            b = self.byte(); r |= (b & 0x7F) << s
            if not b & 0x80:
                return r
            s += 7
        raise ValueError

    def svb(self):
        u = self.uvb()
        return (u >> 1) ^ -(u & 1)

    def var3(self, sel):
        out = []
        for _ in range(3):
            k = sel & 3
            n = (1, 2, 3, 4)[k]
            v = 0
            for i in range(n):
                v |= self.byte() << (8 * i)
            out.append(sext(v, 8 * n)); sel >>= 2
        return out

    def tag2_3s32(self):
        lead = self.byte(); t = lead >> 6
        if t == 0:
            return [sext((lead >> 4) & 3, 2), sext((lead >> 2) & 3, 2), sext(lead & 3, 2)]
        if t == 1:
            b = self.byte()
            return [sext(lead & 15, 4), sext(b >> 4, 4), sext(b & 15, 4)]
        if t == 2:
            return [sext(lead & 63, 6), sext(self.byte() & 63, 6), sext(self.byte() & 63, 6)]
        return self.var3(lead)

    def tag2_3svar(self):
        lead = self.byte(); t = lead >> 6
        if t == 0:
            return [sext((lead >> 4) & 3, 2), sext((lead >> 2) & 3, 2), sext(lead & 3, 2)]
        if t == 1:
            b1 = self.byte()
            return [sext((lead & 0x3E) >> 1, 5), sext(((lead & 1) << 4) | (b1 >> 4), 5), sext(b1 & 15, 4)]
        if t == 2:
            b1 = self.byte(); b2 = self.byte()
            return [sext(((lead & 0x3F) << 2) | (b1 >> 6), 8), sext(((b1 & 0x3F) << 1) | (b2 >> 7), 7), sext(b2 & 0x7F, 7)]
        return self.var3(lead)

    def tag8_4s16(self):
        sel = self.byte(); out = []; nib = 0; buf = 0
        for _ in range(4):
            k = sel & 3
            if k == 0:
                v = 0
            elif k == 1:
                if nib == 0:
                    buf = self.byte(); v = sext(buf >> 4, 4); nib = 1
                else:
                    v = sext(buf & 15, 4); nib = 0
            elif k == 2:
                if nib == 0:
                    v = sext(self.byte(), 8)
                else:
                    c = (buf << 4) & 0xFF; buf = self.byte(); v = sext(c | buf >> 4, 8)
            else:
                if nib == 0:
                    a = self.byte(); b = self.byte(); v = sext(a << 8 | b, 16)
                else:
                    a = self.byte(); b = self.byte(); v = sext(((buf << 12) | (a << 4) | (b >> 4)) & 0xFFFF, 16); buf = b
            out.append(v); sel >>= 2
        return out

    def tag8_8svb(self, n):
        if n == 1:
            return [self.svb()]
        h = self.byte(); out = []
        for _ in range(n):
            out.append(self.svb() if h & 1 else 0); h >>= 1
        return out


def leggi_valori(fl, encs):
    vals = []; i = 0; n = len(encs)
    while i < n:
        e = encs[i]
        if e == 0:
            vals.append(fl.svb()); i += 1
        elif e == 1:
            vals.append(fl.uvb()); i += 1
        elif e == 3:
            vals.append(-sext(fl.uvb(), 14)); i += 1
        elif e == 9:
            vals.append(0); i += 1
        elif e == 6:
            j = i
            while j < n and j - i < 8 and encs[j] == 6:
                j += 1
            vals += fl.tag8_8svb(j - i); i = j
        elif e == 7:
            vals += fl.tag2_3s32()[:n - i]; i += 3
        elif e == 10:
            vals += fl.tag2_3svar()[:n - i]; i += 3
        elif e == 8:
            vals += fl.tag8_4s16()[:n - i]; i += 4
        else:
            raise ValueError
    return vals[:n]


def numeri(s):
    return [int(x) for x in s.split(",") if x != ""]


def decodifica_log(d, inizio, fine):
    h = {}; p = inizio
    while d[p:p + 2] == b"H ":
        e = d.find(b"\n", p)
        k, _, v = d[p + 2:e].decode("latin1").partition(":")
        h[k] = v; p = e + 1
    nomi = h["Field I name"].split(",")
    con_segno = numeri(h["Field I signed"])
    ipred, ienc = numeri(h["Field I predictor"]), numeri(h["Field I encoding"])
    ppred, penc = numeri(h["Field P predictor"]), numeri(h["Field P encoding"])
    altri = {ord(t): numeri(h.get(f"Field {t} encoding", "")) for t in "SGH"}
    minthr, vbatref = int(h.get("minthrottle", "0")), int(h.get("vbatref", "0"))
    minmot = numeri(h.get("motorOutput", "0"))[0]
    imot = nomi.index("motor[0]") if "motor[0]" in nomi else 0
    n = len(nomi)

    def applica(grezzi, pred, prec, prec2):
        out = [0] * n
        for i in range(n):
            pr = pred[i]
            if pr == 0: base = 0
            elif pr == 1: base = prec[i]
            elif pr == 2: base = 2 * prec[i] - prec2[i]
            elif pr == 3:
                s = prec[i] + prec2[i]; base = int(s / 2) if con_segno[i] else s >> 1
            elif pr == 4: base = minthr
            elif pr == 5: base = out[imot]
            elif pr == 6: base = prec[i] + 1
            elif pr == 8: base = 1500
            elif pr == 9: base = vbatref
            elif pr == 11: base = minmot
            else: base = 0
            out[i] = grezzi[i] + base
        return out

    frame = []; prec = prec2 = None
    fl = Flusso(d, p, fine)
    while fl.p < fine:
        pos = fl.p
        try:
            t = fl.byte(); buono = False
            if t == ord("I"):
                f = applica(leggi_valori(fl, ienc), ipred, [0] * n, [0] * n); buono = True
            elif t == ord("P"):
                if prec is None:
                    raise ValueError
                f = applica(leggi_valori(fl, penc), ppred, prec, prec2); buono = True
            elif t in altri:
                leggi_valori(fl, altri[t])
            elif t == ord("E"):
                ev = fl.byte()
                if ev == 255:
                    break
                elif ev in (0, 15, 40):
                    fl.uvb()
                elif ev in (14, 30):
                    fl.uvb(); fl.uvb()
                elif ev == 13:
                    fn = fl.byte()
                    if fn & 128:
                        fl.p += 4
                    else:
                        fl.svb()
                else:
                    raise ValueError
            else:
                raise ValueError
            if fl.p < fine and d[fl.p] not in b"IPSGHE":
                raise ValueError
            if buono:
                if t == ord("I"):
                    prec = prec2 = f
                else:
                    prec2, prec = prec, f
                frame.append(f)
        except (ValueError, EOFError, IndexError):
            prec = prec2 = None
            nxt = d.find(b"I", pos + 1, fine)
            if nxt < 0:
                break
            fl.p = nxt
    return h, nomi, frame


def spettro(x, fs, seg=1024):
    import numpy as np
    x = np.asarray(x, dtype=float)
    finestra = np.hanning(seg); passo = seg // 2
    blocchi = [x[i:i + seg] for i in range(0, len(x) - seg, passo)]
    if not blocchi:
        return None, None
    acc = np.zeros(seg // 2 + 1)
    for b in blocchi:
        acc += np.abs(np.fft.rfft((b - b.mean()) * finestra)) ** 2
    psd = acc / len(blocchi) / (fs * (finestra ** 2).sum())
    return np.fft.rfftfreq(seg, 1 / fs), psd


def installa_librerie():
    """Installa numpy e matplotlib per il Python che sta facendo girare l'app, senza Terminale."""
    import importlib
    import subprocess as sp   # quello vero: nella finestra il nome 'subprocess' dell'app è sostituito
    print("\nPer analizzare i voli servono due librerie di calcolo (numpy e matplotlib).")
    print(f"Il Python che usa l'app (versione {sys.version.split()[0]}) non le ha: succede quando il Mac "
          "installa una versione nuova di Python.")
    if not conferma("Le installo adesso? Serve internet, ci vogliono 1-2 minuti"):
        print("Va bene, nessuna installazione. Senza queste librerie l'analisi del volo non può partire.")
        return False
    for extra in ([], ["--user"]):
        print("Installo numpy e matplotlib" + (" (nella tua cartella utente)" if extra else "") + " ...")
        try:
            proc = sp.Popen([sys.executable, "-m", "pip", "install", "--upgrade", *extra, "numpy", "matplotlib"],
                            stdout=sp.PIPE, stderr=sp.STDOUT, text=True)
        except OSError as e:
            print(f"Non riesco ad avviare l'installazione: {e}")
            return False
        uscita = []
        for riga in proc.stdout:
            uscita.append(riga.rstrip())
            if riga.startswith(("Collecting", "Downloading", "Installing", "Successfully")):
                print(f"\r  {riga.strip()[:90]:<90}", end="", flush=True)
        proc.wait()
        print()
        try:
            with open("fpv_gui_errori.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now():%d/%m/%Y %H:%M:%S} pip (Python {sys.executable})\n" +
                        "\n".join(uscita[-25:]) + "\n")
        except OSError:
            pass
        if proc.returncode == 0:
            importlib.invalidate_caches()
            print("✅ Librerie installate.")
            return True
        if not any("ermission" in r for r in uscita):
            break
    print("❌ L'installazione non è riuscita. Ultime righe:")
    for r in uscita[-6:]:
        print("   " + r)
    print("Controlla di essere collegato a internet e riprova. Se non va, manda queste righe a Claude.")
    return False


def menu_analisi(drone):
    for tentativo in range(2):
        try:
            import numpy as np
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            break
        except ImportError as errore:
            if tentativo:
                # le librerie risultano installate ma non si caricano: mostro e annoto il motivo vero
                import traceback
                print("❌ Le librerie sono installate ma non si caricano. Motivo tecnico:")
                print(f"   {type(errore).__name__}: {errore}")
                try:
                    with open("fpv_gui_errori.log", "a", encoding="utf-8") as f:
                        f.write(f"\n--- {datetime.now():%d/%m/%Y %H:%M:%S} import librerie (Python {sys.version.split()[0]})\n")
                        traceback.print_exc(file=f)
                except OSError:
                    pass
                print("Ho annotato i dettagli: Claude li legge da solo.")
                return
            if not installa_librerie():
                return

    cart = cartella(drone["nome"])
    # Solo i blackbox di QUESTO drone: quelli nella sua cartella, più quelli rimasti nella cartella
    # principale che portano il nome della sua scheda. Il più recente è la scelta proposta.
    files = [os.path.join(cart, f) for f in os.listdir(cart) if f.endswith(".bbl")]
    files += [f for f in file_del_drone_fuori_posto(drone) if f.endswith(".bbl")]
    tutti = sorted(files, key=os.path.getmtime)
    if not tutti:
        print(f"Nessun blackbox di '{drone['nome']}': prima scaricalo con 'Scarica il blackbox'.")
        return
    if len(tutti) == 1:
        percorso = tutti[0]
        print(f"\nAnalizzo il blackbox di '{drone['nome']}': {percorso}")
    else:
        print(f"\nBlackbox di '{drone['nome']}' (dal più vecchio al più recente):")
        for i, f in enumerate(tutti, 1):
            print(f"  {i}. {os.path.basename(f)}  ({os.path.getsize(f) / 1048576:.1f} MB)")
        scelta = chiedi(f"Quale analizzo? [Invio = {len(tutti)}, il più recente]: ")
        percorso = tutti[int(scelta) - 1] if scelta.isdigit() and 0 < int(scelta) <= len(tutti) else tutti[-1]

    d = open(percorso, "rb").read()
    inizi = [m.start() for m in re.finditer(rb"H Product:Blackbox flight data recorder", d)]
    if not inizi:
        print("Il file non contiene log validi.")
        return
    print("\nDecodifico i log (qualche secondo) ...")
    voli = []
    for k, (a, b) in enumerate(zip(inizi, inizi[1:] + [len(d)]), 1):
        h, nomi, fr = decodifica_log(d, a, b)
        if len(fr) < 1000:
            continue
        arr = np.array(fr, dtype=float); ix = {nm: i for i, nm in enumerate(nomi)}
        t = arr[:, ix["time"]]; gas = arr[:, ix["rcCommand[3]"]]
        durata = (t[-1] - t[0]) / 1e6
        in_volo = (gas > 1040).mean()
        troncato = b"End of log" not in d[a:b]
        voli.append((k, h, ix, arr, durata, in_volo, troncato))
    print("\nLog trovati:")
    for k, h, ix, arr, durata, in_volo, troncato in voli:
        tipo = "volo" if in_volo > 0.5 else "a terra / prova"
        print(f"  Log {k:>2}: {durata:5.0f} s  ({tipo}{', interrotto' if troncato else ''})")
    veri = [v for v in voli if v[5] > 0.5]
    if not veri:
        print("Nessun log contiene un volo vero (gas sempre al minimo).")
        return
    predef = max(veri, key=lambda v: v[4])[0]
    scelta = chiedi(f"Quale log analizzo? [Invio = {predef}, il volo più lungo]: ")
    k = int(scelta) if scelta.isdigit() and any(v[0] == int(scelta) for v in voli) else predef
    _, h, ix, arr, durata, _, troncato = next(v for v in voli if v[0] == k)
    analizza_volo(drone, h, ix, arr, durata, k, os.path.basename(percorso), np, plt, troncato)


def risposta_ai_comandi(np, sp, g, fs):
    """Come il drone insegue un comando improvviso (risposta allo scalino), stimata per deconvoluzione
    a finestre, come fanno PIDtoolbox e PID-Analyzer. Calcolata con due regolarizzazioni: se i risultati
    sono vicini e ci sono abbastanza comandi decisi, la stima è affidabile."""
    n = int(fs); passo = n // 4; nr = int(0.5 * fs); w = np.hanning(n)
    stime = []
    for reg in (1e-4, 1e-3):
        R, P = [], []
        for i in range(0, len(sp) - n, passo):
            s = sp[i:i + n]; y = g[i:i + n]
            if np.abs(s).max() < 20:
                continue
            S = np.fft.rfft(s * w); Y = np.fft.rfft(y * w)
            H = Y * np.conj(S) / (np.abs(S) ** 2 + reg * np.mean(np.abs(S) ** 2))
            R.append(np.cumsum(np.fft.irfft(H, n)[:nr])); P.append(np.abs(s).max())
        if not R:
            return None
        R = np.array(R); fin = R[:, int(0.2 * fs):].mean(1); buone = (fin > 0.5) & (fin < 1.5)
        if not buone.any():
            return None
        stime.append(np.average(R[buone], axis=0, weights=np.array(P)[buone]))
    r = stime[1]
    t = np.arange(nr) / fs * 1000
    forti = sum(1 for i in range(0, len(sp) - n, passo) if np.abs(sp[i:i + n]).max() >= 200)
    # ritardo e guadagno: confronto diretto tra comando e rotazione
    lags = range(0, int(0.06 * fs))
    corr = [np.corrcoef(sp[:len(sp) - l], g[l:])[0, 1] for l in lags]
    ritardo = int(np.nanargmax(corr))
    return {"picco": float(r.max()), "picco_alt": float(stime[0].max()), "t_picco": float(t[r.argmax()]),
            "finale": float(r[int(0.2 * fs):].mean()), "forti": forti,
            "ritardo_ms": ritardo / fs * 1000,
            "affidabile": forti >= 8 and abs(r.max() - stime[0].max()) <= 0.08}


def analizza_volo(drone, h, ix, arr, durata, k, nome_file, np, plt, troncato=False):
    comp = leggi_componenti(drone["nome"])
    t = arr[:, ix["time"]]
    fs = 1e6 / np.median(np.diff(t))
    volo = arr[arr[:, ix["rcCommand[3]"]] > 1040]
    consigli_volo = []
    poli = int(h.get("motor_poles", "14") or 14)
    max_motore = numeri(h.get("motorOutput", "158,2047"))
    mmin, mmax = max_motore[0], max_motore[-1]
    rap = []

    # --- rumore
    righe_rumore = []
    curve = []
    for asse, nome in enumerate(["Roll", "Pitch", "Yaw"]):
        chiave_u = f"gyroUnfilt[{asse}]"
        f, pf = spettro(volo[:, ix[f"gyroADC[{asse}]"]], fs)
        pu = spettro(volo[:, ix[chiave_u]], fs)[1] if chiave_u in ix else None
        curve.append((nome, f, pu, pf))
        banda = (f > 80) & (f < 600)
        # Il rumore cala naturalmente con la frequenza: confronto ogni punto con la tendenza
        # dello spettro, così resta solo quello che "sporge" davvero (un picco vero).
        lf, lp = np.log10(f[banda]), 10 * np.log10(pf[banda])
        scarto = lp - np.polyval(np.polyfit(lf, lp, 2), lf)
        residuo = scarto.max()
        riduzione = 10 * np.log10(pu[banda].sum() / pf[banda].sum()) if pu is not None else None
        righe_rumore.append((nome, residuo, riduzione, f[banda][np.argmax(scarto)]))

    if "eRPM[0]" in ix:
        erpm = volo[:, [ix[f"eRPM[{i}]"] for i in range(4)]]
        hz = erpm * 100 / (poli / 2) / 60
        fascia = np.percentile(hz, [5, 95])
        validi = (erpm > 0).mean(0) * 100
        if validi.min() > 95:
            rap.append(("OK", "I giri dei motori sono arrivati per tutto il volo",
                        capito("Tutti e quattro gli ESC hanno comunicato i giri: il filtro RPM ha sempre saputo "
                               "quanto giravano i motori e ha tolto le loro vibrazioni con precisione."), None))
        else:
            mancanti = ", ".join(f"motore {i + 1}" for i, v in enumerate(validi) if v <= 95)
            rap.append(("ATTENZIONE", f"In volo a tratti mancano i giri di: {mancanti}",
                        capito("Su quei motori il filtro RPM, per una parte del volo, non sapeva quanto giravano e "
                               "non poteva togliere le loro vibrazioni.",
                               "Controlla la saldatura del filo segnale tra ESC e scheda di quei motori, e che il "
                               "connettore dello stack sia ben inserito."), None))
    else:
        fascia = None

    peggiore = max(righe_rumore, key=lambda r: r[1])
    if peggiore[1] < 6:
        rap.append(("OK", "Dopo i filtri il segnale è pulito su tutti e tre gli assi",
                    capito("Il sensore di movimento della scheda (giroscopio) non vede vibrazioni anomale: eliche, "
                           "motori e montaggio della scheda sono in buona salute e i filtri fanno il loro lavoro."), None))
        if all(r[2] is not None and r[2] > 15 for r in righe_rumore):
            perche = capito("I filtri tolgono molte vibrazioni e ne restano pochissime. Ogni filtro però aggiunge un "
                            "piccolo ritardo: filtrando un po' meno il drone risponde più pronto ai comandi.",
                            "Si alleggeriscono i filtri a piccoli passi, toccando i motori dopo ogni volo: devono "
                            "restare tiepidi.")
            attuale = h.get("simplified_gyro_filter_multiplier", "")
            if h.get("simplified_gyro_filter", "1") != "0" and attuale.isdigit() and int(attuale) < 150:
                nuovo = min(int(attuale) + 10, 150)
                consigli_volo.append(consiglio(
                    "simplified_gyro_filter_multiplier", attuale, nuovo, "Filtri del giroscopio un po' più leggeri",
                    perche, f"cursore filtri giroscopio da {attuale} a {nuovo} (+10, un passo piccolo): i filtri "
                    "lasciano passare un po' più di segnale e aggiungono meno ritardo", PROVA_FILTRI))
                rap.append(("NOTA", "Si può filtrare un po' meno",
                            perche + f" La correzione è pronta in 'Correggi e migliora': cursore filtri da {attuale} a {nuovo}.", None))
            else:
                rap.append(("NOTA", "Si può filtrare un po' meno", perche, None))
    else:
        rap.append(("ATTENZIONE", f"Resta una vibrazione sul {peggiore[0].lower()} anche dopo i filtri",
                    capito(f"C'è una vibrazione precisa intorno a {peggiore[3]:.0f} Hz che i filtri non tolgono. Di "
                           "solito viene dalla meccanica: un'elica rovinata o sbilanciata, un motore con i cuscinetti "
                           "stanchi, una vite allentata o un braccio del frame che vibra.",
                           "Controlla la meccanica prima di toccare i filtri: eliche nuove, motori che girano liberi "
                           "a mano senza grattare, viti del frame e della scheda strette."), None))

    # --- KV: i giri misurati in volo sono compatibili con il KV scritto nei componenti?
    # Serve un "punch out" (gas a tutta per almeno mezzo secondo): nei picchi brevi i motori non fanno
    # in tempo a prendere giri e il confronto non avrebbe senso. Sotto sforzo un motore gira sempre MENO
    # di KV × tensione: se gira di più, il KV scritto (o il numero di poli) è sbagliato.
    kv = kv_motori(comp)
    if kv and "eRPM[0]" in ix and "vbatLatest" in ix:
        gas_stick = arr[:, ix["rcCommand[3]"]]
        a_tutta = gas_stick >= 1950
        tratti, inizio_t = [], None
        for n_, su in enumerate(np.append(a_tutta, False)):
            if su and inizio_t is None:
                inizio_t = n_
            elif not su and inizio_t is not None:
                if t[n_ - 1] - t[inizio_t] >= 0.5e6:
                    tratti.append((inizio_t, n_))
                inizio_t = None
        if tratti:
            migliori = []
            for a_, b_ in tratti:
                giri_t = arr[a_:b_, [ix[f"eRPM[{m_}]"] for m_ in range(4)]] * 100 / (poli / 2)
                tens_t = arr[a_:b_, ix["vbatLatest"]] / 100
                rapp_t = giri_t.max(axis=1) / (kv * np.maximum(tens_t, 1))
                migliori.append((np.percentile(rapp_t, 95), giri_t.max()))
            rapp, giri_max = max(migliori)
            if rapp > 1.05:
                rap.append(("VERIFICA", f"I motori girano più di quanto permetta il KV scritto ({kv} KV)",
                            capito(f"Nelle accelerate a tutto gas arrivano a circa {migliaia(giri_max)} giri al minuto, "
                                   f"il {rapp * 100:.0f}% dei giri teorici (KV × tensione). Con le eliche montate un "
                                   "motore gira sempre meno del teorico: quindi il KV vero è più alto di quello "
                                   "scritto, oppure il numero di poli è sbagliato.",
                                   "Controlla il KV sul motore e i poli nei Componenti: un KV più alto del previsto "
                                   "vuol dire motori più caldi."), None))
            else:
                rap.append(("OK", f"I giri dei motori sono compatibili con il KV ({kv} KV)",
                            capito(f"Nelle accelerate a tutto gas arrivano a circa {migliaia(giri_max)} giri al minuto, "
                                   f"il {rapp * 100:.0f}% dei giri teorici (KV × tensione): con le eliche montate "
                                   "deve restare sotto il 100%."), None))

    # --- motori
    mot = volo[:, [ix[f"motor[{i}]"] for i in range(4)]]
    lavoro = (mot.mean(0) - mmin) / (mmax - mmin) * 100
    # schema Betaflight: 1 post. dx, 2 ant. dx, 3 post. sx, 4 ant. sx
    davanti, dietro = lavoro[[1, 3]].mean(), lavoro[[0, 2]].mean()
    destra, sinistra = lavoro[[0, 1]].mean(), lavoro[[2, 3]].mean()
    diag_a, diag_b = lavoro[[0, 3]].mean(), lavoro[[1, 2]].mean()
    diff_fb = (davanti - dietro) / ((davanti + dietro) / 2) * 100
    diff_lr = (destra - sinistra) / ((destra + sinistra) / 2) * 100
    diff_yaw = (diag_a - diag_b) / ((diag_a + diag_b) / 2) * 100
    if abs(diff_fb) > 10:
        lato = "anteriori" if diff_fb > 0 else "posteriori"
        rap.append(("ATTENZIONE", f"I motori {lato} lavorano il {abs(diff_fb):.0f}% in più",
                    capito("Il drone è più pesante da quel lato (baricentro spostato) e quei motori compensano di "
                           "continuo: si scaldano di più e consumi più batteria.",
                           f"Sposta la batteria un po' più {'indietro' if diff_fb > 0 else 'avanti'} e rifai un volo "
                           "per vedere se si pareggia."), None))
    if abs(diff_lr) > 10:
        lato = "destri" if diff_lr > 0 else "sinistri"
        rap.append(("ATTENZIONE", f"I motori {lato} lavorano il {abs(diff_lr):.0f}% in più",
                    capito("Il peso è spostato su un lato: quei motori compensano di continuo.",
                           "Controlla che la batteria sia dritta e centrata e che nulla (antenne, GPS, telecamera) "
                           "sia montato tutto da una parte."), None))
    if abs(diff_yaw) > 10:
        rap.append(("VERIFICA", f"Una diagonale di motori lavora il {abs(diff_yaw):.0f}% in più dell'altra",
                    capito("Il drone tende a girare su se stesso e i motori lo trattengono di continuo.",
                           "Controlla che i motori siano dritti sui bracci, che le eliche siano tutte uguali e "
                           "integre, e che un motore non giri più duro degli altri."), None))
    if abs(diff_fb) <= 10 and abs(diff_lr) <= 10 and abs(diff_yaw) <= 10:
        rap.append(("OK", "I quattro motori lavorano in modo simile",
                    capito("Il drone è ben bilanciato: peso centrato, motori ed eliche in ordine."), None))

    saturo = (mot.max(1) >= mmax - 5).mean() * 100
    if saturo > 1:
        rap.append(("ATTENZIONE", f"Per il {saturo:.1f}% del volo almeno un motore era al massimo",
                    capito("In quei momenti il drone non ha più margine per correggere e può perdere stabilità "
                           "nelle manovre.",
                           "Le cause tipiche sono tre: drone troppo pesante per i motori, batteria stanca o "
                           "scarica, regolazioni (PID) troppo aggressive. Parti dalla batteria: provane una in "
                           "buono stato."), None))
    else:
        rap.append(("OK", "C'è potenza di riserva",
                    capito(f"I motori non arrivano quasi mai al massimo; il più impegnato lavora in media al "
                           f"{lavoro.max():.0f}%. Il drone ha sempre margine per correggere."), None))

    # --- batteria
    # Due regole diverse: durante le accelerate la tensione crolla per un attimo (sotto sforzo);
    # a fine volo, con poco gas, conta la tensione a cui atterri (regola: LiPo e LiHV 3,5 V, Li-ion 3,3 V).
    celle = comp.get("celle_batteria", "")
    prof = profilo_batteria(comp)
    soglia, atterra = prof["minimo_volo"], prof["atterraggio"]
    if celle.isdigit() and "vbatLatest" in ix:
        vb = arr[:, ix["vbatLatest"]] / 100 / int(celle)
        gas_su = np.where(arr[:, ix["rcCommand[3]"]] > 1040)[0]
        minima = np.percentile(vb[gas_su], 1) if len(gas_su) else np.percentile(vb, 1)
        finale = None
        if len(gas_su) and not troncato:
            fine = t[gas_su[-1]]
            ultimi = (t >= fine - 3e6) & (t <= fine)   # ultimi 3 secondi in aria
            finale = float(np.median(vb[ultimi]))
        if minima < soglia:
            rap.append(("ATTENZIONE", f"Sotto sforzo la batteria è scesa a {volt(minima)} V per cella",
                        capito(f"Nelle accelerate la tensione crolla per un attimo, ma per una {prof['nome']} è "
                               f"troppo: {prof['fonte_minimo']}. Le celle soffrono e perdono capacità.",
                               "Atterra prima, evita le accelerate a fine batteria, o usa batterie in miglior stato."),
                        None))
        elif prof["calo_forte"] and minima < prof["calo_forte"]:
            rap.append(("VERIFICA", f"Nelle accelerate la batteria è scesa a {volt(minima)} V per cella",
                        capito(f"Un calo momentaneo fino a {volt(prof['calo_forte'])} V nelle accelerate è normale; "
                               "più in basso vuol dire batteria stanca, troppo piccola per il drone o accelerate a "
                               "fine batteria. Ripetuto spesso, invecchia le celle.",
                               "Prova con una batteria più fresca; se il calo resta, a fine batteria evita le "
                               "accelerate a tutto gas."), None))
        else:
            rap.append(("OK", f"Sotto sforzo la batteria non è mai scesa sotto {volt(soglia)} V per cella",
                        capito(f"Il punto più basso, nelle accelerate, è stato {volt(minima)} V per cella: le celle "
                               "non hanno sofferto."), None))
        if finale is None:
            rap.append(("NOTA", "Non so a che tensione sei atterrato",
                        capito("La registrazione si è fermata prima dell'atterraggio, quindi la tensione a fine volo "
                               "non c'è."), None))
        elif finale < atterra:
            rap.append(("ATTENZIONE", f"Sei atterrato con {volt(finale)} V per cella",
                        capito(f"Per le {prof['nome']} si atterra a {volt(atterra)} V per cella "
                               f"({prof['fonte_atterraggio']}): sotto, le celle invecchiano più in fretta e la "
                               "batteria dura meno.",
                               f"Atterra quando l'OSD segna {volt(atterra)} V per cella. Se l'avviso di batteria bassa "
                               "arriva troppo tardi, controlla la configurazione."), None))
        else:
            rap.append(("OK", f"Sei atterrato con {volt(finale)} V per cella",
                        capito(f"Sopra i {volt(atterra)} V per cella della regola per le {prof['nome']}: la batteria "
                               "è stata trattata bene."), None))

    # --- risposta ai comandi (base del tuning dei PID)
    # tratto continuo dal decollo all'atterraggio: niente pezzi incollati, che falserebbero i tempi
    in_aria = np.where(arr[:, ix["rcCommand[3]"]] > 1040)[0]
    tratto = arr[in_aria[0]:in_aria[-1] + 1] if len(in_aria) else arr
    risposte = {}
    for asse, nome in enumerate(["Roll", "Pitch", "Yaw"]):
        if f"setpoint[{asse}]" in ix:
            risposte[nome] = risposta_ai_comandi(np, tratto[:, ix[f"setpoint[{asse}]"]], tratto[:, ix[f"gyroADC[{asse}]"]], fs)
    valide = {n: r for n, r in risposte.items() if r}
    if valide:
        elenco = ", ".join(f"{n.lower()} {r['ritardo_ms']:.0f} ms" for n, r in valide.items())
        lenti = [n for n, r in valide.items() if n != "Yaw" and r["ritardo_ms"] > 15]
        if not lenti:
            rap.append(("OK", f"Il drone risponde pronto agli stick ({elenco})",
                        capito("È il tempo tra quando muovi lo stick e quando il drone si muove: sotto i 15 "
                               "millesimi di secondo è una risposta pronta."), None))
        else:
            rap.append(("VERIFICA", f"Il drone risponde lento agli stick ({elenco})",
                        capito("È il tempo tra quando muovi lo stick e quando il drone si muove; su roll e pitch un "
                               "drone ben messo sta sotto i 15 millesimi di secondo. Può dipendere da un volo con pochi "
                               "comandi decisi (la misura è meno precisa), da filtri molto pesanti, da feedforward basso "
                               "o da motori al limite.",
                               "Fai un volo di taratura con comandi decisi (le manovre sono indicate in 'Correggi e "
                               "migliora') e rianalizzalo."), None))
    affidabili = {n: r for n, r in valide.items() if r["affidabile"] and n != "Yaw"}
    if not affidabili:
        rap.append(("NOTA", "Per giudicare le regolazioni (PID) servono comandi più decisi",
                    capito("In questo volo i colpi di stick decisi sono pochi (" +
                           ", ".join(f"{n.lower()} {r['forti']} su 8" for n, r in valide.items() if n != "Yaw") +
                           "): non posso giudicare i PID con sicurezza, quindi non propongo modifiche.",
                           "Al prossimo volo: " + "; ".join(m[0].lower() + m[1:] for m in MANOVRE_TARATURA) + "."), None))
    pids_attivi = h.get("simplified_pids_mode", "2") != "0"
    for nome, r in affidabili.items():
        sup = (r["picco"] - 1) * 100
        if r["picco"] > 1.15:
            attuale = h.get("simplified_d_gain", "")
            if pids_attivi and attuale.isdigit():
                nuovo = int(attuale) + 10
                perche = capito(f"Su un colpo di stick il {nome.lower()} va oltre del {sup:.0f}% prima di fermarsi "
                                "e poi torna indietro: un piccolo rimbalzo. Un po' più di D (la parte dei PID che "
                                "frena) lo smorza.",
                                "Aumenta il D di un passo, vola e tocca i motori: devono restare tiepidi.")
                consigli_volo.append(consiglio(
                    "simplified_d_gain", attuale, nuovo, f"Meno rimbalzo sul {nome.lower()}", perche,
                    f"cursore D da {attuale} a {nuovo} (+10, circa +10% di D): più smorzamento, motori un po' "
                    "più caldi", MANOVRE_TARATURA + ["Appena atterrato tocca i motori: tiepidi, non bollenti"]))
                rap.append(("VERIFICA", f"{nome}: il drone va oltre il comando e rimbalza", perche +
                            f" La correzione è pronta in 'Correggi e migliora': cursore D da {attuale} a {nuovo}.", None))
        else:
            rap.append(("OK", f"{nome}: si ferma dove chiedi, senza rimbalzi",
                        capito(f"Dopo un colpo di stick arriva al punto giusto (va oltre solo del "
                               f"{max(sup, 0):.0f}%): le regolazioni su questo asse sono buone."), None))
    if troncato:
        rap.append(("NOTA", "Il volo è registrato solo in parte",
                    capito("La memoria del blackbox si è riempita durante il volo e la registrazione si è fermata.",
                           "Prima dei prossimi voli svuotala con 'Cancella i log dal drone' (dopo averli scaricati)."), None))

    # --- confronto con il volo prima dell'ultima modifica
    voce = {"data": f"{datetime.now():%d/%m/%Y %H:%M}", "file": os.path.basename(nome_file), "log": k,
            "parametri": {c: v for c, v in h.items() if c.startswith(PARAMETRI_VOLO)},
            "metriche": metriche_volo(curve, valide, lavoro, saturo, durata, np, volo, ix)}
    confronto = None
    precedenti = [v for v in leggi_storico(drone) if (v["file"], v["log"]) != (voce["file"], voce["log"])]
    for prima in reversed(precedenti):
        if prima["parametri"] != voce["parametri"]:
            confronto = (prima,) + confronta_voli(prima, voce)
            break
        if prima["metriche"].get("durata", 0) >= 20:
            break   # l'ultimo volo valido ha le stesse impostazioni: nessuna modifica da giudicare
    salva_nello_storico(drone, voce)

    # --- grafico
    nome_png = os.path.join(cartella(drone["nome"]), f"analisi_log{k}_{datetime.now():%Y-%m-%d_%H-%M}.png")
    BLU, ARA, INK, SEC, FONDO = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e", "#fcfcfb"
    plt.rcParams.update({"text.color": INK, "axes.labelcolor": SEC, "xtick.color": SEC,
                         "ytick.color": SEC, "axes.edgecolor": "#d6d5d0"})
    fig = plt.figure(figsize=(12, 8.6), facecolor=FONDO)
    gs = fig.add_gridspec(2, 3, height_ratios=[1.25, 1], hspace=0.55, wspace=0.12)
    for i, (nome, f, pu, pf) in enumerate(curve):
        ax = fig.add_subplot(gs[0, i], facecolor=FONDO)
        if fascia is not None:
            ax.axvspan(*fascia, color="#e3e2dd", lw=0)
        if pu is not None:
            ax.plot(f, 10 * np.log10(pu), color=ARA, lw=2, label="Prima dei filtri")
        ax.plot(f, 10 * np.log10(pf), color=BLU, lw=2, label="Dopo i filtri")
        ax.set_xlim(0, min(1000, fs / 2)); ax.set_ylim(-60, 25)
        ax.set_title(nome, loc="left", fontweight="bold"); ax.set_xlabel("Frequenza (Hz)")
        ax.grid(axis="y", color="#ecebe7", lw=0.8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        if i == 0:
            ax.set_ylabel("Rumore del giroscopio (dB)")
        else:
            ax.set_yticklabels([])
    mani, etichette = ax.get_legend_handles_labels()
    fig.legend(mani, etichette, loc="upper right", bbox_to_anchor=(0.98, 0.985), ncol=2, frameon=False)
    fig.text(0.06, 0.955, f"{drone['nome']}  ·  log {k}, {durata:.0f} s", fontsize=15, fontweight="bold")
    fig.text(0.06, 0.925, "Fascia grigia: frequenza di rotazione dei motori in volo", color=SEC, fontsize=10.5)
    ax2 = fig.add_subplot(gs[1, :], facecolor=FONDO)
    ordine = [1, 3, 0, 2]
    nomi_m = ["Motore 1\nposteriore destro", "Motore 2\nanteriore destro",
              "Motore 3\nposteriore sinistro", "Motore 4\nanteriore sinistro"]
    barre = ax2.bar(range(4), [lavoro[i] for i in ordine], color=[BLU, BLU, "#86b6ef", "#86b6ef"], width=0.55)
    ax2.set_xticks(range(4)); ax2.set_xticklabels([nomi_m[i] for i in ordine])
    for b, i in zip(barre, ordine):
        ax2.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.4, f"{lavoro[i]:.1f}%", ha="center", color=INK)
    ax2.set_ylabel("Lavoro medio (%)"); ax2.set_ylim(0, max(lavoro) * 1.25)
    ax2.set_title("Quanto lavora in media ogni motore", loc="left", fontweight="bold")
    ax2.grid(axis="y", color="#ecebe7", lw=0.8); ax2.set_axisbelow(True)
    for s in ("top", "right"):
        ax2.spines[s].set_visible(False)
    fig.savefig(nome_png, dpi=130, facecolor=FONDO, bbox_inches="tight")
    plt.close(fig)

    print(f"\n=== Analisi del log {k} ({durata:.0f} s) ===")
    stampa_rapporto(rap)
    if confronto:
        prima, cambiati, voci, peggio, meglio = confronto
        print("\n" + "=" * 56)
        print("  PRIMA E DOPO LA MODIFICA")
        print("=" * 56)
        print(f"Confronto con il volo del {prima['data']} (log {prima['log']}, {prima['metriche'].get('durata', 0)} s).")
        print("Impostazioni cambiate tra i due voli:")
        for c, a_, b_ in cambiati:
            print(f"  {nome_parametro_volo(c)}: {a_} → {b_}")
        motivi = voli_confrontabili(prima["metriche"], voce["metriche"])
        if motivi:
            voci, peggio, meglio = [], [], []
            print("\n🔍 NON POSSO GIUDICARE LA MODIFICA: i due voli sono troppo diversi per un confronto onesto.")
            for m_ in motivi:
                print(f"  • {m_.capitalize()}")
            print("    Cosa fare: Fai un volo di prova simile a quello di prima (almeno 2 minuti, stesse manovre, "
                  "batteria carica) e rianalizzalo.")
            esito_c = None
        else:
            stampa_rapporto(voci)
        if motivi:
            pass
        elif peggio and not meglio:
            esito_c = ("❌ LA MODIFICA NON HA AIUTATO. Peggiorati: " + ", ".join(n.lower() for n in peggio) +
                       ". Ti propongo di tornare indietro.")
            for c, a_, b_ in cambiati:
                if c.startswith("simplified_") and str(a_).lstrip("-").isdigit():
                    consigli_volo[:] = [x for x in consigli_volo if x["param"] != c]   # niente consigli opposti
                    consigli_volo.append(consiglio(
                        c, b_, a_, f"Torna a com'era: {nome_parametro_volo(c)}",
                        capito("Il volo dopo la modifica è andato peggio di quello prima.",
                               "Rimetti il valore di prima e rifai un volo di prova."),
                        f"{nome_parametro_volo(c)} da {b_} a {a_} (il valore di prima)", PROVA_FILTRI))
        elif meglio and not peggio:
            esito_c = "✅ LA MODIFICA HA MIGLIORATO IL DRONE. Migliorati: " + ", ".join(n.lower() for n in meglio) + "."
        elif meglio:
            esito_c = ("🔍 RISULTATO MISTO. Migliorati: " + ", ".join(n.lower() for n in meglio) + ". Peggiorati: " +
                       ", ".join(n.lower() for n in peggio) + ". Decidi tu cosa conta di più per come voli.")
        else:
            esito_c = "ℹ️ NESSUNA DIFFERENZA CHIARA tra prima e dopo: la modifica si può tenere o togliere."
        if esito_c:
            print("\n" + esito_c)
            print("  (Ho controllato che i due voli siano simili per durata, gas e manovre. Il vento non lo vedo: "
                  "confronta voli fatti in condizioni simili.)")
        rap.extend((lv, "Prima e dopo: " + t, p, r) for lv, t, p, r in voci if lv != "NOTA")
    salva_consigli(drone, "volo", consigli_volo)
    salva_rapporto(drone, "volo", rap, f"log {k} di {nome_file}, {durata:.0f} s")
    if consigli_volo:
        print(f"\n{'Una correzione pronta' if len(consigli_volo) == 1 else f'{len(consigli_volo)} correzioni pronte'}: "
              "le trovi in 'Correggi e migliora', con il volo di prova.")
    print(f"\nGrafico salvato in: {nome_png}")
    if sys.platform == "darwin":
        subprocess.run(["open", nome_png])



# ============================================================== confronto prima / dopo

# Impostazioni registrate nel blackbox che cambiano il comportamento in volo:
# se cambiano tra due voli, quei due voli sono un "prima" e un "dopo".
PARAMETRI_VOLO = ("simplified_", "rollPID", "pitchPID", "yawPID", "d_min", "ff_weight", "gyro_lpf1_dyn_hz",
                  "gyro_lpf2_static_hz", "dterm_lpf1_dyn_hz", "dterm_lpf2_static_hz", "dyn_notch_count",
                  "rpm_filter_harmonics", "tpa_rate", "anti_gravity_gain", "rates", "rc_rates", "rc_expo")
NOMI_HEADER = {"rollPID": "PID del roll (P, I, D)", "pitchPID": "PID del pitch (P, I, D)",
               "yawPID": "PID dello yaw (P, I, D)", "d_min": "D minimo (roll, pitch, yaw)",
               "ff_weight": "Feedforward (roll, pitch, yaw)", "gyro_lpf1_dyn_hz": "Filtro giroscopio 1 (Hz)",
               "gyro_lpf2_static_hz": "Filtro giroscopio 2 (Hz)", "dterm_lpf1_dyn_hz": "Filtro del D 1 (Hz)",
               "dterm_lpf2_static_hz": "Filtro del D 2 (Hz)", "dyn_notch_count": "Filtri notch dinamici",
               "rpm_filter_harmonics": "Armoniche del filtro RPM", "tpa_rate": "TPA (riduzione PID a gas alto)",
               "anti_gravity_gain": "Anti gravity", "rates": "Rate", "rc_rates": "RC rate", "rc_expo": "Expo"}


def file_storico(drone):
    return os.path.join(cartella(drone["nome"]), "storico_voli.json")


def leggi_storico(drone):
    try:
        return json.load(open(file_storico(drone), encoding="utf-8"))
    except (OSError, ValueError):
        return []


def salva_nello_storico(drone, voce):
    """Aggiunge (o aggiorna, se lo stesso volo era già stato analizzato) un volo allo storico."""
    storico = [v for v in leggi_storico(drone) if (v["file"], v["log"]) != (voce["file"], voce["log"])]
    storico.append(voce)
    json.dump(storico[-50:], open(file_storico(drone), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def nome_parametro_volo(k):
    return NOMI_PARAMETRI.get(k) or NOMI_HEADER.get(k) or k


def confronta_voli(prima, dopo):
    """Confronta due voli con impostazioni diverse. Ritorna (cambiati, voci del rapporto, peggiorato, migliorato)."""
    cambiati = [(k, prima["parametri"].get(k, "-"), dopo["parametri"].get(k, "-"))
                for k in sorted(set(prima["parametri"]) | set(dopo["parametri"]))
                if prima["parametri"].get(k) != dopo["parametri"].get(k)]
    voci, meglio, peggio = [], [], []
    pm, dm = prima["metriche"], dopo["metriche"]

    def giudica(nome, a, b, soglia, unita, meno_e_meglio, significato_meglio, significato_peggio, fare_peggio,
                db=False):
        if a is None or b is None or (unita.startswith(" %") and max(a, b) < 0.5):
            return
        diff = b - a
        if db:   # vibrazioni: in percentuale, più chiara dei decibel
            perc = (10 ** (diff / 10) - 1) * 100
            rapporto_ = 10 ** (diff / 10)
            quanto = (f"circa {rapporto_:.0f} volte tanto" if rapporto_ >= 2
                      else f"il {abs(perc):.0f}% in {'meno' if perc < 0 else 'più'}")
            da, a_, unita = "", "", ""
        else:
            quanto = None
            da, a_ = f"{a:.1f}".replace(".", ","), f"{b:.1f}".replace(".", ",")
        tra = f" ({quanto})" if quanto else f" ({da} → {a_}{unita})"
        if abs(diff) < soglia:
            voci.append(("NOTA", f"{nome}: praticamente uguale{tra}",
                         capito("La differenza è troppo piccola per dire che la modifica abbia cambiato qualcosa "
                                "su questo punto."), None))
        elif (diff < 0) == meno_e_meglio:
            meglio.append(nome)
            voci.append(("OK", f"{nome}: migliorato{tra}", capito(significato_meglio), None))
        else:
            peggio.append(nome)
            voci.append(("ATTENZIONE", f"{nome}: peggiorato{tra}",
                         capito(significato_peggio, fare_peggio), None))

    sul = {"Roll": "sul roll", "Pitch": "sul pitch", "Yaw": "sullo yaw"}
    for asse in ("Roll", "Pitch", "Yaw"):
        giudica(f"Vibrazioni rimaste {sul[asse]}", pm["rumore"].get(asse), dm["rumore"].get(asse), 3.0, " dB",
                True, "Dopo i filtri resta meno vibrazione: segnale più pulito, motori più freschi.",
                "Dopo i filtri resta più vibrazione: i motori lavorano più sporchi e possono scaldarsi.",
                "Tocca i motori dopo il volo: se sono più caldi di prima, torna all'impostazione precedente.",
                db=True)
    for asse in ("Roll", "Pitch"):
        giudica(f"Prontezza sul {asse.lower()}", pm["ritardo"].get(asse), dm["ritardo"].get(asse), 1.0, " ms",
                True, "Il drone risponde prima agli stick.",
                "Il drone risponde più tardi agli stick.",
                "Se la modifica serviva a togliere vibrazioni ed è riuscita, un ritardo un po' maggiore è il prezzo; "
                "altrimenti torna all'impostazione precedente.")
        giudica(f"Rimbalzo sul {asse.lower()}", pm["rimbalzo"].get(asse), dm["rimbalzo"].get(asse), 5.0, " %",
                True, "Dopo un colpo di stick il drone si ferma più pulito, con meno rimbalzo.",
                "Dopo un colpo di stick il drone va più oltre e rimbalza di più.",
                "Torna all'impostazione precedente, oppure fai un altro volo di taratura per conferma.")
    giudica("Motori al massimo", pm.get("saturo"), dm.get("saturo"), 1.0, " % del volo", True,
            "I motori arrivano meno spesso al massimo: più margine per correggere.",
            "I motori arrivano più spesso al massimo: meno margine per correggere.",
            "Controlla che sia stato un volo simile al precedente; se sì, torna all'impostazione precedente.")
    return cambiati, voci, peggio, meglio


def metriche_volo(curve, valide, lavoro, saturo, durata, np, volo, ix):
    # Vibrazione rimasta dopo i filtri: energia totale tra 80 e 600 Hz, in dB (più stabile del singolo picco).
    rumore = {}
    for nome, f, _pu, pf in curve:
        banda = (f > 80) & (f < 600)
        rumore[nome] = round(float(10 * np.log10(pf[banda].sum() + 1e-12)), 1)
    return {"rumore": rumore,
            "ritardo": {n: round(float(r["ritardo_ms"]), 1) for n, r in valide.items() if r["affidabile"]},
            "rimbalzo": {n: round(max(float(r["picco"]) - 1, 0) * 100, 1)
                         for n, r in valide.items() if r["affidabile"] and n != "Yaw"},
            "lavoro_max": round(float(lavoro.max()), 1), "saturo": round(float(saturo), 1),
            "durata": round(float(durata)),
            "gas_medio": round(float((volo[:, ix["rcCommand[3]"]].mean() - 1000) / 10), 1),
            "attivita": round(float(np.abs(volo[:, [ix[f"setpoint[{a}]"] for a in (0, 1)]]).mean()), 1)
            if "setpoint[0]" in ix else None}


def voli_confrontabili(pm, dm):
    """I due voli si possono confrontare solo se sono simili: abbastanza lunghi, stesso gas, stesse manovre."""
    motivi = []
    if min(pm.get("durata", 0), dm.get("durata", 0)) < 60:
        motivi.append("almeno uno dei due voli dura meno di un minuto")
    if pm.get("gas_medio") is not None and dm.get("gas_medio") is not None and \
            abs(pm["gas_medio"] - dm["gas_medio"]) > 8:
        motivi.append(f"il gas medio è diverso ({pm['gas_medio']:.0f}% contro {dm['gas_medio']:.0f}%)")
    if pm.get("attivita") and dm.get("attivita") and not 0.6 <= dm["attivita"] / pm["attivita"] <= 1.6:
        motivi.append("le manovre sono molto diverse (uno dei due voli è molto più movimentato)")
    if "gas_medio" not in pm:
        motivi.append("il volo di prima è stato analizzato con una versione vecchia dell'app")
    return motivi



# ============================================================== libreria dei preset ufficiali di Betaflight
# Fonte: l'archivio ufficiale github.com/betaflight/firmware-presets, scaricato come ZIP nella cartella dell'app.
# I preset non sono regole: sono tarature pubblicate da tuner noti, utili come riferimento.

import zipfile

STATO_PRESET = {"OFFICIAL": "ufficiale", "COMMUNITY": "della comunità", "EXPERIMENTAL": "sperimentale"}
ORDINE_STATO = {"OFFICIAL": 0, "COMMUNITY": 1, "EXPERIMENTAL": 2}


def _analizza_preset(testo, percorso):
    """Legge un file preset: intestazione, impostazioni di base e opzioni (con quelle preselezionate)."""
    p = {"percorso": percorso, "titolo": "", "autore": "", "stato": "", "categoria": "", "parole": "",
         "versione": "", "base": {}, "opzioni": []}
    opzione = None
    gruppo = ""
    for riga in testo.splitlines():
        r = riga.strip()
        if r.startswith("#$"):
            chiave, _, valore = r[2:].partition(":")
            chiave, valore = chiave.strip(), valore.strip()
            if chiave == "TITLE":
                p["titolo"] = valore
            elif chiave == "AUTHOR":
                p["autore"] = valore
            elif chiave == "STATUS":
                p["stato"] = valore.upper()
            elif chiave == "CATEGORY":
                p["categoria"] = valore.upper()
            elif chiave == "KEYWORDS":
                p["parole"] = valore
            elif chiave == "FIRMWARE_VERSION" and not p["versione"]:
                p["versione"] = valore
            elif chiave.startswith("OPTION_GROUP BEGIN"):
                gruppo = r.split(":", 1)[1].strip() if ":" in r else ""
            elif chiave.startswith("OPTION_GROUP END"):
                gruppo = ""
            elif chiave.startswith("OPTION BEGIN"):
                testo_op = r.split(":", 1)[1].strip() if ":" in r else ""
                opzione = {"gruppo": gruppo, "nome": testo_op, "scelta": "(CHECKED)" in r.upper(), "set": {}}
                p["opzioni"].append(opzione)
            elif chiave.startswith("OPTION END"):
                opzione = None
            continue
        m = re.match(r"set\s+(\w+)\s*=\s*(.+)$", r)
        if m:
            nome, valore = m.group(1).lower(), m.group(2).strip()
            (opzione["set"] if opzione else p["base"])[nome] = valore
    return p


def file_preset_zip():
    candidati = sorted(f for f in os.listdir(".") if f.startswith("firmware-presets") and f.endswith(".zip"))
    return candidati[-1] if candidati else None


def carica_libreria(versione="4.5"):
    """Tutti i preset TUNE e FILTERS di una versione di Betaflight, più i valori predefiniti."""
    zf = file_preset_zip()
    if not zf:
        return None
    lib = {"fonte": zf, "predefiniti": {}, "preset": []}
    with zipfile.ZipFile(zf) as z:
        for nome in z.namelist():
            parti = nome.split("/")
            if len(parti) < 4 or parti[1] != "presets" or parti[2] != versione or not nome.endswith(".txt"):
                continue
            if parti[3] not in ("tune", "filters", "bnf"):
                continue
            testo = z.read(nome).decode("utf-8", "replace")
            p = _analizza_preset(testo, "/".join(parti[2:]))
            if parti[3] in ("tune", "filters") and parti[-1] == "defaults.txt":
                lib["predefiniti"].update(p["base"])
                continue
            if p["titolo"]:
                lib["preset"].append(p)
    return lib


def valori_preset(lib, p, rpm=False):
    """Valori effettivi del preset come lo applicherebbe il Configurator: predefiniti + base + opzioni
    preselezionate. Se il drone ha il filtro RPM, aggiunge anche l'opzione del preset per il filtro RPM."""
    v = dict(lib["predefiniti"])
    v.update(p["base"])
    for o in p["opzioni"]:
        if o["scelta"]:
            v.update(o["set"])
    if rpm:
        for o in p["opzioni"]:
            if "rpm" in o["nome"].lower() and "no rpm" not in o["nome"].lower() and \
                    "without" not in o["nome"].lower():
                v.update(o["set"])
    return {k: (str(int(x)) if x.isdigit() else x) for k, x in v.items()}


def pollici_eliche(testo):
    m = re.search(r"(\d+(?:[.,]\d)?)\s*(?:\"|pollic|inch|in\b|”|'')", testo or "", re.I) or \
        re.search(r"^\s*(\d+(?:[.,]\d)?)\b", testo or "")
    return float(m.group(1).replace(",", ".")) if m else None


def misure_del_preset(p):
    """Le misure di elica a cui si riferisce un preset, lette da titolo e parole chiave."""
    t = f"{p['titolo']} {p['parole']}".lower()
    misure = set()
    for a, b in re.findall(r"(\d+(?:\.\d)?)\s*(?:\"|in\b|inch)?\s*(?:/|-|to)\s*(\d+(?:\.\d)?)\s*(?:\"|in\b|inch|”)", t):
        misure.update(x / 2 for x in range(int(float(a) * 2), int(float(b) * 2) + 1))
    for x in re.findall(r"(\d+(?:\.\d)?)\s*(?:\"|in\b|inch|”|\s+inch)", t):
        misure.add(float(x))
    for x in re.findall(r"aos[ _](\d+(?:\.\d)?)\b", t):
        misure.add(float(x))
    if "whoop" in t and not misure:
        misure.update({1.0, 1.2})
    return {m for m in misure if 1 <= m <= 13}


def riferimenti_per(drone, lib, quanti=4):
    """I preset più vicini al drone: stessa misura di elica (o la più vicina), stesso telaio se c'è,
    prima gli ufficiali."""
    comp = leggi_componenti(drone["nome"])
    misura = pollici_eliche(comp.get("eliche", ""))
    frame = comp.get("frame", "").lower()
    scelti = []
    for p in lib["preset"]:
        if p["categoria"] != "TUNE":
            continue
        punti = ORDINE_STATO.get(p["stato"], 3) * 10
        testo = f"{p['titolo']} {p['parole']}".lower()
        stesso_telaio = any(len(w) > 3 and w in testo for w in re.findall(r"[a-z0-9]+", frame))
        misure = misure_del_preset(p)
        if misura and misure:
            distanza = min(abs(misura - m) for m in misure)
            if distanza > 1 and not stesso_telaio:
                continue
            punti += distanza * 15
        elif not stesso_telaio:
            continue
        if stesso_telaio:
            punti -= 40
        if any(w in testo for w in ("race", "racer", "200mph", "whoop", "cine")) and "race" not in frame:
            punti += 12   # tarature da gara o da cinewhoop: meno adatte a un freestyle / long range
        scelti.append((punti, p, stesso_telaio))
    scelti.sort(key=lambda x: x[0])
    return [(p, t) for _, p, t in scelti[:quanti]], misura


CURSORI = [
    ("simplified_master_multiplier", "Forza generale dei PID"),
    ("simplified_pi_gain", "Reattività (P e I)"),
    ("simplified_d_gain", "Smorzamento (D)"),
    ("simplified_feedforward_gain", "Prontezza agli stick (feedforward)"),
    ("simplified_i_gain", "Tenuta della traiettoria (I)"),
    ("simplified_pitch_pi_gain", "Pitch rispetto al roll (P e I)"),
    ("simplified_pitch_d_gain", "Pitch rispetto al roll (D)"),
    ("simplified_gyro_filter_multiplier", "Filtro del giroscopio (più alto = filtra meno)"),
    ("simplified_dterm_filter_multiplier", "Filtro del D (più alto = filtra meno)"),
    ("dyn_notch_count", "Filtri notch dinamici"),
    ("tpa_rate", "TPA (riduzione dei PID a gas alto)"),
    ("anti_gravity_gain", "Anti gravity"),
    ("thrust_linear", "Thrust linear"),
]


def menu_riferimenti(drone):
    """Confronta le regolazioni del drone con i preset di Betaflight più simili."""
    lib = carica_libreria()
    if not lib:
        print("Non trovo l'archivio dei preset di Betaflight (firmware-presets-master.zip) nella cartella dell'app.")
        print("Scaricalo da github.com/betaflight/firmware-presets (pulsante Code, poi Download ZIP).")
        return
    percorso, diff, _porte = None, None, None
    cart = cartella(drone["nome"])
    diffs = sorted(f for f in os.listdir(cart) if f.startswith("diff_"))
    if not diffs:
        print("Prima fai il controllo della Configurazione: mi serve leggere le regolazioni attuali del drone.")
        return
    attuali = dict(lib["predefiniti"])
    for riga in open(os.path.join(cart, diffs[-1]), encoding="utf-8", errors="replace"):
        m = re.match(r"\s*set\s+(\w+)\s*=\s*(.+)$", riga.strip())
        if m:
            attuali[m.group(1).lower()] = m.group(2).strip()
    scelti, misura = riferimenti_per(drone, lib)
    attuali = {k: (str(int(x)) if x.isdigit() else x) for k, x in attuali.items()}
    rpm = attuali.get("dshot_bidir", "OFF").upper() in ("ON", "1")
    print(f"\nRIFERIMENTI DAI PRESET DI BETAFLIGHT 4.5")
    print(f"Archivio: {lib['fonte']}  ({len(lib['preset'])} preset letti)")
    print(f"Il tuo drone: eliche {str(misura or '?').replace('.0', '')} pollici, filtro RPM "
          f"{'attivo' if rpm else 'spento'} · regolazioni lette il {diffs[-1][5:15]}")
    if attuali.get("simplified_pids_mode", "RPY").upper() in ("OFF", "0"):
        print("Nota: i cursori dei PID sul tuo drone sono spenti (PID scritti a mano), quindi il confronto dei "
              "cursori vale solo come indicazione.")
    if not scelti:
        print("\nNessun preset per una misura simile. Indica la misura delle eliche nei Componenti.")
        return
    print("\nI preset non sono regole: sono tarature di tuner esperti per droni simili al tuo.")
    print("Servono a capire in che direzione andare, un passo alla volta, sempre con il volo di prova.")
    for p, telaio in scelti:
        v = valori_preset(lib, p, rpm)
        print(f"\n• {p['titolo']}")
        print(f"    Cosa significa: Preset {STATO_PRESET.get(p['stato'], p['stato'].lower())} di {p['autore']}"
              f"{', pensato proprio per il tuo telaio' if telaio else ''}. File: {p['percorso']}.")
        righe = []
        for k, nome in CURSORI:
            a, b = attuali.get(k), v.get(k)
            if a is None or b is None or a.upper() == b.upper():
                continue
            righe.append(f"{nome}: tuo {a}, preset {b}")
        if righe:
            print("    Cosa fare: Ecco dove il tuo drone è diverso da questo preset" +
                  (" (con l'opzione del preset per il filtro RPM):" if rpm else ":"))
            for r_ in righe:
                print(f"    – {r_}")
        else:
            print("    Cosa fare: Le regolazioni principali coincidono con questo preset.")
    print("\nLe opzioni dei preset (pulizia del drone, filtro RPM, frequenza degli ESC) cambiano i filtri:")
    print("in Betaflight Configurator, scheda Presets, puoi vederle tutte prima di applicare qualcosa.")


# ============================================================== 4. test ESC

MSP_SET_REBOOT = 68


def leggi_telemetria(ser):
    r = msp(ser, MSP_MOTOR_TELEMETRY)
    if not r:
        return None
    motori = []
    for i in range(r[0]):
        rpm, err, temp, volt, corr, cons = struct.unpack("<IHBHHH", r[1 + i * 13:14 + i * 13])
        motori.append((rpm, err / 100, temp, volt / 100))
    return motori


def scegli_batteria(drone):
    """Chiede quale batteria si sta per attaccare, proponendo quella dei componenti.
    Se nei componenti il tipo non c'era, lo salva."""
    comp = leggi_componenti(drone["nome"])
    attuale = profilo_batteria(comp)["nome"] if comp.get("tipo_batteria") else "LiPo"
    nomi = ["LiPo", "LiHV", "Li-ion"]
    celle = comp.get("celle_batteria", "")
    print()
    print("  1. LiPo     piena a 4,20 V per cella")
    print("  2. LiHV     alta tensione, piena a 4,35 V per cella")
    print("  3. Li-ion   celle tipo 18650 o 21700")
    risposta = chiedi(f"Che batteria stai per attaccare{f' ({celle} celle)' if celle.isdigit() else ''}? "
                      f"[Invio = {nomi.index(attuale) + 1}, {attuale}]: ")
    scelto = nomi[int(risposta) - 1] if risposta in ("1", "2", "3") else attuale
    if not comp.get("tipo_batteria"):
        valori = dict(comp)
        valori["tipo_batteria"] = scelto
        scrivi_componenti(drone["nome"], valori)
        print(f"Salvato nei componenti: batteria {scelto}.")
    elif scelto != attuale:
        print(f"Per questo test uso le soglie {scelto}; nei componenti resta {attuale}.")
    return PROFILI_BATTERIA[scelto]


def menu_test_esc(drone):
    print("\nTEST ESC")
    prof = scegli_batteria(drone)
    comp_s = leggi_componenti(drone["nome"])
    celle_s = comp_s.get("celle_batteria", "")
    for chiave_c, nome_c in (("esc_celle_max", "gli ESC"), ("fc_celle_max", "la scheda di volo")):
        mx = comp_s.get(chiave_c, "")
        if celle_s.isdigit() and mx.isdigit() and int(celle_s) > int(mx):
            print(f"\n❌ NON ATTACCARE LA BATTERIA: il drone usa una {celle_s}S, ma {nome_c} reggono al massimo {mx}S.")
            print("   Si brucerebbero appena colleghi la batteria. Controlla i Componenti o la scheda tecnica.")
            rap = [("ATTENZIONE", f"Test fermato: batteria {celle_s}S, ma {nome_c} reggono al massimo {mx}S",
                    capito("Una tensione più alta di quella per cui sono costruiti li brucia appena attacchi la "
                           "batteria.", f"Usa batterie fino a {mx}S o correggi i Componenti se il valore è sbagliato."),
                    None)]
            salva_rapporto(drone, "esc", rap)
            return rap
    print(f"\nSoglie {prof['nome']}: piena {volt(prof['piena'])} V, storage {volt(prof['storage'])} V per cella.")
    print(" 1. SMONTA LE ELICHE (i motori non girano, ma è la regola d'oro).")
    print(" 2. Lascia il cavo USB collegato e attacca la batteria.")
    print(" Il test dura circa 30 secondi in tutto, poi stacca subito la batteria.")
    print(" Con la radio spenta i motori possono fare dei bip: è normale.")
    if not conferma("Eliche smontate e batteria attaccata?"):
        print("Annullato.")
        return None

    # Gli ESC impiegano qualche secondo ad avviarsi dopo che attacchi la batteria:
    # aspetto che rispondano (fino a 10 s) prima di iniziare a misurare.
    comp_b = leggi_componenti(drone["nome"])
    celle_attese = comp_b.get("celle_batteria", "")
    with apri() as ser:
        analog0 = msp(ser, MSP_ANALOG)
    tensione0 = struct.unpack("<H", analog0[7:9])[0] / 100 if analog0 and len(analog0) >= 9 else 0
    if tensione0 and celle_attese.isdigit():
        per_cella = tensione0 / int(celle_attese)
        if per_cella > prof["anomala_sopra"]:
            for n in range(int(celle_attese) + 1, 9):
                if tensione0 / n <= prof["anomala_sopra"]:
                    break
            if per_cella <= 4.45 and prof["nome"] != "LiHV":
                ipotesi = "sembra una LiHV (alta tensione) o una batteria caricata troppo"
            else:
                ipotesi = f"sembra una batteria da {n} celle"
            print(f"\n❌ STACCA SUBITO LA BATTERIA: leggo {volt(tensione0)} V, cioè {volt(per_cella)} V per cella con le "
                  f"{celle_attese} celle di questo drone. Una {prof['nome']} piena arriva a {volt(prof['piena'])} V per "
                  f"cella: {ipotesi}.")
            print("   Test interrotto. Controlla la batteria prima di riattaccarla.")
            rap = [("ATTENZIONE", f"Batteria sbagliata: {tensione0:.2f} V su un drone da {celle_attese} celle",
                    capito(f"Sono {volt(per_cella)} V per cella, ma una {prof['nome']} piena arriva a "
                           f"{volt(prof['piena'])}: {ipotesi}. ESC e "
                           "regolatori di tensione possono rovinarsi con una tensione oltre le loro specifiche, anche "
                           "a motori fermi. Ho interrotto subito il test.",
                           f"Stacca la batteria e usa una {celle_attese} celle. Se la batteria era giusta, controlla "
                           "nei Componenti il numero di celle e il tipo di batteria."), None)]
            salva_rapporto(drone, "esc", rap)
            return rap
    print("Aspetto che gli ESC finiscano l'avvio ...")
    pronti = False
    with apri() as ser:
        inizio = time.time()
        while time.time() - inizio < 10:
            m = leggi_telemetria(ser)
            if m and not all(x[1] >= 99.9 for x in m):
                pronti = True
                break
            time.sleep(1)
        if not pronti:
            print("Gli ESC non rispondono: riavvio la scheda con la batteria attaccata (circa 10 s) ...")
            try:
                msp(ser, MSP_SET_REBOOT, b"\x00", attesa=1)
            except (TimeoutError, IOError):
                pass
    if not pronti:
        attendi_riavvio()
        time.sleep(4)

    letture, analog = [], None
    with apri() as ser:
        time.sleep(1)
        inizio = time.time()
        while time.time() - inizio < 15:
            m = leggi_telemetria(ser)
            if m:
                letture.append(m)
            analog = msp(ser, MSP_ANALOG) or analog
            print(f"\r  Misura in corso ... {max(15 - (time.time() - inizio), 0):4.0f} s ", end="", flush=True)
            time.sleep(1)
    print("\n\nPuoi staccare la batteria.\n")
    # Con il DShot bidirezionale spento Betaflight risponde lo stesso, ma con tutti zeri:
    # zero errori non vuol dire "comunicazione perfetta", vuol dire "nessun dato".
    tutti_zero = bool(letture) and all(rpm == 0 and err == 0 and t == 0 for l in letture for rpm, err, t, _v in l)
    senza_dati = not letture or tutti_zero
    bidir_spento = leggi_rilevati(drone).get("bidir") is False
    if senza_dati:
        print("Gli ESC non mandano dati alla scheda.")
        letture = [[(0, 100.0, 0, 0)] * 4]   # segnaposto: la parte ESC qui sotto viene saltata

    n = len(letture[0])
    # errori: valore tipico (mediana) e peggiore, così un singolo istante non falsa il risultato
    errori = [sorted(l[i][1] for l in letture)[len(letture) // 2] for i in range(n)]
    errori_max = [max(l[i][1] for l in letture) for i in range(n)]
    # temperatura: valida solo se plausibile (fino a 100 °C) e presente in almeno metà delle letture
    temperature, anomale = [], []
    for i in range(n):
        valori = [l[i][2] for l in letture if l[i][2]]
        buoni = [v for v in valori if v <= 100]
        if len(buoni) >= len(letture) / 2:
            temperature.append(sorted(buoni)[len(buoni) // 2])
        else:
            temperature.append(0)
            if valori:
                anomale.append(f"motore {i + 1}: {max(valori)} °C")
    if not senza_dati:
        print(f"Motori letti: {n}  ({len(letture)} letture)")
    for i in range(n if not senza_dati else 0):
        print(f"  Motore {i + 1}: errori tipici {errori[i]:.2f}% (peggiore {errori_max[i]:.2f}%)   "
              f"temperatura {temperature[i] or '-'} °C")
    tensione = struct.unpack("<H", analog[7:9])[0] / 100 if analog and len(analog) >= 9 else 0
    celle = comp_b.get("celle_batteria", "")
    if tensione:
        extra = f"  ({volt(tensione / int(celle))} V per cella)" if celle.isdigit() else ""
        print(f"Tensione batteria: {volt(tensione)} V{extra}")

    rap = []
    # --- batteria (la scheda legge solo il totale: le singole celle si controllano con il checker)
    if tensione and celle.isdigit():
        cella = tensione / int(celle)
        if cella > prof["anomala_sopra"] or cella < prof["impossibile_sotto"]:
            rap.append(("ATTENZIONE", f"Tensione impossibile: {volt(cella)} V per cella",
                        capito(f"Con {celle} celle {prof['nome']}, {volt(tensione)} V non sono un valore reale: o la "
                               "batteria è di un altro tipo o ha un numero di celle diverso, o la scheda misura male "
                               "la tensione.",
                               "Controlla nei Componenti celle e tipo di batteria. Se sono giusti, misura la batteria con il "
                               "checker e confronta: se la scheda sbaglia, va tarata in Betaflight Configurator "
                               "(scheda Alimentazione e batteria)."), None))
        elif cella >= prof["carica_da"]:
            rap.append(("OK", f"Batteria carica: {volt(cella)} V per cella ({prof['nome']})",
                        capito(f"Una {prof['nome']} piena arriva a {volt(prof['piena'])} V per cella. È la media: la "
                               "scheda vede solo la tensione totale, non le singole celle.",
                               "Le singole celle controllale con il checker, come fai già."), None))
        elif cella >= prof["scarica_sotto"]:
            rap.append(("NOTA", f"Batteria non carica: {volt(cella)} V per cella ({prof['nome']})",
                        capito(f"Per una {prof['nome']} lo storage è circa {volt(prof['storage'])} V per cella: va "
                               "bene per le prove al banco, non per volare.",
                               "Caricala prima di uscire a volare."), None))
        else:
            rap.append(("ATTENZIONE", f"Batteria scarica: {volt(cella)} V per cella ({prof['nome']})",
                        capito(f"È sotto la tensione di storage di una {prof['nome']} (circa "
                               f"{volt(prof['storage'])} V): lasciata così le celle si rovinano.",
                               "Ricaricala almeno fino allo storage."), None))
    elif tensione:
        rap.append(("NOTA", f"Batteria: {tensione:.2f} V in totale",
                    capito("Non so quante celle ha questo drone, quindi non posso dirti se è carica.",
                           "Indica il numero di celle nei Componenti."), None))
    if senza_dati:
        if bidir_spento:
            rap.append(("VERIFICA", "Gli ESC non mandano dati: il DShot bidirezionale è spento",
                        capito("Senza DShot bidirezionale gli ESC non rispondono alla scheda, quindi non posso "
                               "controllare la comunicazione con gli ESC e il filtro RPM non può lavorare.",
                               "Attiva il DShot bidirezionale (la correzione è pronta in Correggi e migliora) e "
                               "ripeti questo test."), None))
        else:
            rap.append(("VERIFICA", "Gli ESC non mandano nessun dato alla scheda",
                        capito("Senza questi dati non posso controllare gli ESC e il filtro RPM non sa quanto girano "
                               "i motori.",
                               "In Betaflight Configurator, scheda Configurazione, controlla che il DShot "
                               "bidirezionale sia attivo, poi ripeti il test."), None))
    elif all(e >= 99.9 for e in errori):
        rap.append(("VERIFICA", "A motori fermi gli ESC non rispondono",
                    capito("Tutti e quattro i motori segnano 100% di errori: non è un segnale sporco ma proprio "
                           "nessuna risposta (con una saldatura difettosa i valori cambierebbero da motore a motore). "
                           "Con alcune combinazioni di scheda ed ESC i dati arrivano solo con i motori che girano.",
                           "La prova che conta è il volo: dopo un volo usa 'Analizza un volo' e controlla che i "
                           "giri dei motori siano arrivati."), None))
    elif max(errori) < 1:
        rap.append(("OK", "Gli ESC parlano con la scheda senza errori",
                    capito("Gli errori di comunicazione (DShot bidirezionale) sono sotto l'1% su tutti i motori: il "
                           "filtro RPM riceve dati puliti."), None))
    else:
        peggiori = [f"motore {i + 1}: {e:.1f}%" for i, e in enumerate(errori) if e >= 1]
        rap.append(("ATTENZIONE", f"Errori di comunicazione con gli ESC ({', '.join(peggiori)})",
                    capito("Il filtro RPM su quei motori riceve dati sporchi e toglie peggio le vibrazioni.",
                           "Controlla la saldatura del filo segnale di quei motori e che il firmware degli ESC "
                           "(Bluejay) sia aggiornato."), None))
    if senza_dati:
        pass
    elif any(temperature):
        rap.append(("OK", f"Gli ESC comunicano la temperatura ({max(temperature)} °C)",
                    capito("I dati estesi degli ESC (EDT) sono attivi: puoi vedere la temperatura degli ESC "
                           "sull'OSD."), None))
    elif not all(e >= 99.9 for e in errori):
        significa = ("I dati estesi degli ESC (EDT) non sembrano attivi, quindi la temperatura degli ESC sull'OSD "
                     "resta vuota. Il volo non ne risente.")
        if anomale:
            significa += (f" Ho scartato valori impossibili a motori fermi ({', '.join(anomale)}): letture "
                          "sporadiche, non un ESC caldo.")
        rap.append(("NOTA", "Gli ESC non comunicano la temperatura",
                    capito(significa, "Se la vuoi: con Bluejay 0.21 o più recente si attiva in Betaflight con il "
                                      "parametro dshot_edt; con versioni più vecchie aggiorna prima Bluejay."), None))
    stampa_rapporto(rap)
    salva_rapporto(drone, "esc", rap)
    return rap


# ============================================================== 6. modifica parametro

# Parametri che toccano la sicurezza: dopo la modifica serve una prova funzionale.
SICUREZZA = [
    (("failsafe_", "gps_rescue_", "rx_", "serialrx_", "rc_smoothing", "mincheck", "maxcheck", "small_angle"),
     "ricevente",
     "Tocca il collegamento radio, il failsafe o l'arm: esegui il test della ricevente (Ricevente) prima di volare."),
    (("vbat_", "force_battery_cell_count"),
     "esc",
     "Tocca gli avvisi della batteria: esegui il test ESC e batteria e controlla che l'OSD mostri il numero di celle "
     "giusto prima di volare."),
    (("motor_", "dshot_", "yaw_motors_reversed", "mixer"),
     "esc",
     "Tocca i motori: esegui il test ESC (ESC e batteria) e, prima del volo, verifica il senso di rotazione dei motori "
     "nella scheda Motori del Configurator, SENZA eliche."),
    (("p_", "i_", "d_", "f_", "d_min", "gyro_", "dterm_", "dyn_", "rpm_filter", "simplified_", "tpa_",
      "iterm_", "anti_gravity", "feedforward"),
     "volo",
     "Tocca PID o filtri: primo volo di prova in area libera, hover basso pronto a disarmare. Dopo il volo "
     "tocca i motori (non devono scottare) e analizza il log (Analizza un volo)."),
]


def classe_sicurezza(nome):
    for prefissi, tipo, consiglio in SICUREZZA:
        if any(nome == p or nome.startswith(p) for p in prefissi):
            return tipo, consiglio
    return None, None


def get_righe(cli, nome):
    """Come 'get nome', ma funziona anche per le funzioni (feature) di Betaflight,
    che si leggono con 'feature' e si scrivono con 'feature X' / 'feature -X'.
    Nell'app una funzione si chiama feature_<nome>, con valore ON oppure OFF."""
    if not nome.startswith("feature_"):
        return cli.comando(f"get {nome}")
    sigla = nome[len("feature_"):].upper()
    attive = set()
    for r in cli.comando("feature"):
        if r.strip().lower().startswith("enabled:"):
            attive.update(r.split(":", 1)[1].split())
    return [f"{nome} = {'ON' if sigla in attive else 'OFF'}", "Allowed values: OFF, ON"]


def imposta(cli, nome, valore):
    if not nome.startswith("feature_"):
        return cli.comando(f"set {nome} = {valore}")
    if str(valore).upper() not in ("ON", "OFF"):
        return ["Invalid value: usa ON oppure OFF"]
    sigla = nome[len("feature_"):].upper()
    return cli.comando(f"feature {'' if str(valore).upper() == 'ON' else '-'}{sigla}")


def valore_letto(righe, nome):
    """Dal risultato di 'get' estrae il valore del parametro ESATTO (get elenca anche i nomi simili)."""
    for r in righe:
        m = re.match(rf"\s*{re.escape(nome)}\s*=\s*(.*)$", r)
        if m:
            return m.group(1).strip()
    return None


def blocco_parametro(righe, nome):
    """Righe da mostrare: solo il parametro esatto e i suoi limiti (Allowed range, valori ammessi...)."""
    out, dentro = [], False
    for r in righe:
        if re.match(rf"\s*{re.escape(nome)}\s*=", r):
            dentro = True
            out.append(r)
        elif dentro:
            if not r.strip() or re.match(r"\s*\w+\s*=", r):
                break
            out.append(r)
    return out


def uguali(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


# Cosa cambia davvero sul drone quando si sposta un cursore: lo rileggo prima e dopo, come prova.
EFFETTI = {
    "simplified_d_gain": ["d_roll", "d_pitch", "d_min_roll", "d_min_pitch"],
    "simplified_pi_gain": ["p_roll", "i_roll", "p_pitch", "i_pitch"],
    "simplified_i_gain": ["i_roll", "i_pitch", "i_yaw"],
    "simplified_feedforward_gain": ["f_roll", "f_pitch", "f_yaw"],
    "simplified_master_multiplier": ["p_roll", "d_roll", "p_pitch", "d_pitch"],
    "simplified_gyro_filter_multiplier": ["gyro_lpf1_dyn_min_hz", "gyro_lpf1_dyn_max_hz", "gyro_lpf2_static_hz"],
    "simplified_dterm_filter_multiplier": ["dterm_lpf1_dyn_min_hz", "dterm_lpf1_dyn_max_hz", "dterm_lpf2_static_hz"],
}


def leggi_effetti(cli, nome):
    return {p: valore_letto(cli.comando(f"get {p}"), p) for p in EFFETTI.get(nome, [])}


def eta(quando):
    giorni = int((_tempo.time() - quando) // 86400)
    return "oggi" if giorni <= 0 else "ieri" if giorni == 1 else f"{giorni} giorni fa"


def diagnosi(drone):
    """Mette insieme tutto quello che l'app sa del drone e stampa cosa non va e come sistemarlo.
    Ritorna l'elenco delle correzioni che l'app può applicare."""
    rapporti = leggi_rapporti(drone)
    consigli = leggi_consigli(drone)
    print("\n▶ Diagnosi del drone")
    print("  Unisco i risultati di tutti i controlli fatti su questo drone.")
    print("\nCONTROLLI SU CUI SI BASA")
    mancanti = []
    for tipo, nome in FONTI.items():
        r = rapporti.get(tipo)
        if not r:
            print(f"  ⏭️ {nome}: mai eseguito")
            mancanti.append(tipo)
            continue
        gravi = sum(1 for x in r["rap"] if x[0] == "ATTENZIONE")
        extra = f", {r['extra']}" if r.get("extra") else ""
        stato = ("1 problema" if gravi == 1 else f"{gravi} problemi") if gravi else "nessun problema"
        simbolo = "⚠️" if gravi else "✅"
        print(f"  {simbolo} {nome}: {r['data']} ({eta(r['quando'])}{extra}) — {stato}")
        if _tempo.time() - r["quando"] > 30 * 86400:
            mancanti.append(tipo)

    da_risolvere, da_verificare = [], []
    for tipo, r in rapporti.items():
        for livello, titolo, perche in r["rap"]:
            if livello == "ATTENZIONE":
                da_risolvere.append((titolo, perche, FONTI.get(tipo, tipo)))
            elif livello == "VERIFICA":
                da_verificare.append((titolo, perche, FONTI.get(tipo, tipo)))
    per_titolo = {c["titolo"]: c for c in consigli}

    if da_risolvere:
        print("\n⚠️  DA RISOLVERE")
        for titolo, perche, fonte in da_risolvere:
            c = per_titolo.get(titolo)
            print(f"  • {titolo}")
            print(rientra(perche))
            print(f"    Trovato da: {fonte}." + (f" Posso correggerlo io: {nome_umano(c['param'])} da {c['attuale']} a {c['consigliato']}."
                                               if c else " Questo si sistema a mano, come indicato sopra."))
    if da_verificare:
        print("\n🔍 DA VERIFICARE")
        for titolo, perche, fonte in da_verificare:
            print(f"  • {titolo}")
            print(rientra(perche))
            print(f"    Trovato da: {fonte}.")
    migliorie = [c for c in consigli if c["titolo"] not in {t for t, _, _ in da_risolvere}]
    if migliorie:
        print("\nℹ️  MIGLIORIE POSSIBILI")
        for c in migliorie:
            print(f"  • {c['titolo']}")
            print(rientra(c["perche"]))
            print(f"    Posso farlo io: {nome_umano(c['param'])} da {c['attuale']} a {c['consigliato']}.")
    if mancanti:
        print("\n🔍 CONTROLLI DA FARE")
        spiega = {"configurazione": "premi Configurazione (basta il cavo USB)",
                  "ricevente": "premi Ricevente (radio accesa, niente batteria)",
                  "esc": "premi ESC e batteria (batteria attaccata, senza eliche)",
                  "volo": "dopo un volo: Scarica il blackbox, poi Analizza un volo"}
        for tipo in mancanti:
            quando = "mai eseguito" if tipo not in rapporti else "eseguito più di 30 giorni fa"
            print(f"  • {FONTI[tipo]}: {quando}")
            print(f"    Per una diagnosi completa {spiega[tipo]}. Oppure usa Controllo in officina, che li fa in fila.")
    if not (da_risolvere or da_verificare or consigli):
        if mancanti:
            print("\nNei controlli fatti finora non ho trovato problemi, ma la diagnosi non è completa: mancano dei controlli.")
        else:
            print("\n✅ Nessun problema e nessuna correzione da fare: secondo tutti i controlli il drone è a posto.")
    return consigli


def menu_correggi(drone):
    consigli = diagnosi(drone)
    if not consigli:
        print("\nNon ci sono correzioni da applicare al drone in questo momento.")
        if not conferma("Vuoi comunque modificare un parametro a mano? (solo per chi conosce Betaflight)"):
            return None
        return modifica_libera(drone)
    print("\nCORREZIONI CHE POSSO APPLICARE AL DRONE")
    for i, c in enumerate(consigli, 1):
        print(f"  {i}. {c['titolo']} — {nome_umano(c['param'])}: da {c['attuale']} a {c['consigliato']}")
    print(f"  {len(consigli) + 1}. Parametro a scelta (solo per esperti di Betaflight)")
    print("  0. Nessuna, per ora")
    sc = chiedi("Quale correzione vuoi applicare? [Invio = 1]: ")
    if sc == "0":
        print("Nessuna modifica.")
        return None
    if sc == str(len(consigli) + 1):
        return modifica_libera(drone)
    if sc == "" or (sc.isdigit() and 1 <= int(sc) <= len(consigli)):
        scelto = consigli[int(sc or 1) - 1]
    else:
        print("Scelta non valida: nessuna modifica.")
        return None
    print(f"\n▶ {scelto['titolo']}")
    print(f"  Cosa cambio: {nome_umano(scelto['param'])} (in Betaflight: {scelto['param']})")
    print(f"  Da {scelto['attuale']} a {scelto['consigliato']}")
    print(rientra(scelto["perche"], "  ") if scelto["perche"].startswith("Cosa significa") else f"  Perché: {scelto['perche']}")
    print(f"  Di quanto: {scelto['quanto']}")
    print("  Prima salvo una copia della configurazione; dopo verifico due volte che la modifica sia rimasta.")
    return applica_modifica(drone, scelto["param"], scelto)


def modifica_libera(drone):
    print("\n▶ PARAMETRO A SCELTA (per esperti)")
    print("  Scrivi il nome di un parametro di Betaflight, lo stesso che useresti nella CLI del Configurator.")
    print("  Ti mostro il valore attuale e i limiti prima di chiederti quello nuovo.")
    nome = chiedi("Nome del parametro di Betaflight (es. craft_name): ").lower()
    if not nome:
        print("Nessuna modifica.")
        return None
    return applica_modifica(drone, nome, None)


def menu_modifica(drone):   # nome storico: ora la voce del menu è 'Correggi e migliora'
    return menu_correggi(drone)


def applica_modifica(drone, nome, scelto):
    if drone.get("scheda") == "non collegato":
        print("Per applicare una correzione il drone deve essere collegato con il cavo USB: collegalo e premi Ricollega.")
        return None
    if not re.fullmatch(r"[a-z0-9_]+", nome):
        print("Nome non valido: solo lettere minuscole, numeri e trattino basso.")
        return None
    momento = datetime.now()
    with CLI() as cli:
        print("Salvo una copia della configurazione attuale ...")
        backup = cli.comando("diff all", attesa=20)
        percorso_backup = os.path.join(cartella(drone["nome"]), f"backup_{momento:%Y-%m-%d_%H-%M-%S}.txt")
        open(percorso_backup, "w", encoding="utf-8").write("\n".join(backup) + "\n")
        print(f"Copia salvata in: {percorso_backup}")
        info = get_righe(cli, nome)
        prima = valore_letto(info, nome)
        if prima is None:
            print(f"Il parametro '{nome}' non esiste in questa versione di Betaflight. Nessuna modifica.")
            simili = [r.split("=")[0].strip() for r in info if "=" in r]
            if simili:
                print("Parametri con un nome simile: " + ", ".join(simili[:8]))
            return None
        print("\n  " + "\n  ".join(blocco_parametro(info, nome)) + "\n")
        tipo, avviso = classe_sicurezza(nome)
        if tipo:
            print(f"⚠️  Parametro di sicurezza. {avviso}\n")
        if scelto:
            nuovo = scelto["consigliato"]   # il valore lo propone l'analisi: resta solo la conferma
        else:
            nuovo = chiedi("Nuovo valore (Invio per annullare): ")
        if not nuovo:
            print("Annullato, nessuna modifica.")
            return None
        if uguali(nuovo, prima):
            print("Il valore è già questo: nessuna modifica.")
            return None
        domanda = (f"Applico al drone: {nome_umano(nome)} da {prima} a {nuovo}?" if scelto
                   else f"Confermi: {nome_umano(nome)} da {prima} a {nuovo}, e salvataggio sul drone?")
        if not conferma(domanda):
            print("Annullato, nessuna modifica.")
            return None
        esito = imposta(cli, nome, nuovo)
        if any("Invalid" in r or "ERROR" in r for r in esito):
            print("Betaflight ha rifiutato il valore:\n  " + "\n  ".join(esito))
            print("Nessuna modifica salvata.")
            return None
        # --- verifica 1: rilettura subito, PRIMA di salvare
        letto1 = valore_letto(get_righe(cli, nome), nome)
        if not uguali(letto1, nuovo):
            print(f"❌ Verifica 1 fallita: ho scritto '{nuovo}' ma rileggo '{letto1}'. NON salvo nulla:")
            print("   al riavvio la scheda torna alla configurazione di prima.")
            return None
        print(f"✅ Verifica 1 (subito): il drone conferma {nome} = {letto1}")
        effetti_prima = leggi_effetti(cli, nome)
        cli.salva = True
        print("Salvo sul drone (la scheda si riavvia) ...")

    # --- verifica 2: rilettura dopo il riavvio
    with CLI() as cli:
        letto2 = valore_letto(get_righe(cli, nome), nome)
        effetti_dopo = leggi_effetti(cli, nome)
    ok2 = uguali(letto2, nuovo)
    if ok2:
        print(f"✅ Verifica 2 (dopo il riavvio): il valore è rimasto salvato, {nome} = {letto2}")
    else:
        print(f"❌ Verifica 2 fallita: dopo il riavvio {nome} vale '{letto2}' invece di '{nuovo}'.")
        print(f"   La modifica non è stata salvata correttamente. La copia di prima è: {percorso_backup}")

    effetto_ok = True
    if effetti_prima:
        cambiati = [f"{p} {effetti_prima[p]} -> {effetti_dopo.get(p)}" for p in effetti_prima
                    if effetti_prima[p] != effetti_dopo.get(p)]
        if cambiati:
            print("✅ Effetto sul drone: " + ", ".join(cambiati))
        else:
            effetto_ok = False
            print("⚠️  Il cursore è stato salvato ma i valori collegati non sono cambiati: forse i cursori "
                  "semplificati non sono attivi (simplified_pids_mode / simplified_gyro_filter). Nessun effetto in volo.")

    registro = os.path.join(cartella(drone["nome"]), "registro_modifiche.txt")
    with open(registro, "a", encoding="utf-8") as f:
        f.write(f"{momento:%d/%m/%Y %H:%M}  {nome}: '{prima}' -> '{nuovo}'  "
                f"verifica1=OK verifica2={'OK' if ok2 else 'FALLITA'}  backup={os.path.basename(percorso_backup)}\n")
    print(f"Modifica annotata in: {registro}")
    print(f"Per tornare indietro: copia di prima in {percorso_backup}")

    if ok2 and scelto and effetto_ok and scelto.get("al_banco"):
        print("\nPROVA DA FARE PRIMA DI VOLARE")
        for i, passo in enumerate(scelto["prova"], 1):
            print(f"  {i}. [ ] {passo}")
    elif ok2 and scelto and effetto_ok:
        togli_consiglio(drone, nome)
        print("\nVOLO DI PROVA PER VERIFICARE IL MIGLIORAMENTO")
        for i, passo in enumerate(scelto["prova"], 1):
            print(f"  {i}. [ ] {passo}")
        print("Al ritorno: Scarica il blackbox e Analizza un volo. Se il risultato peggiora, torna al valore di prima "
              f"({scelto['attuale']}) con 'Correggi e migliora'.")
    if ok2 and tipo and not scelto:
        print(f"\n⚠️  Prova funzionale richiesta: {avviso}")
        if tipo == "ricevente" and conferma("Eseguo adesso il test della ricevente?"):
            menu_test_rx(drone)
        elif tipo == "esc" and conferma("Eseguo adesso il test ESC?"):
            menu_test_esc(drone)
    return None


# ============================================================== 8. test della ricevente

MSP_MODE_RANGES = 34
MSP_RC = 105
MSP_STATUS_EX = 150
BIT_FAILSAFE, BIT_RXLOSS = 1 << 1, 1 << 2

NOMI_MODI = {
    0: "ARM", 1: "ANGLE", 2: "HORIZON", 3: "ALTHOLD", 6: "HEADFREE", 13: "BEEPER", 15: "LEDLOW",
    19: "OSD DISABLE", 26: "BLACKBOX", 27: "FAILSAFE", 28: "AIR MODE", 29: "3D",
    30: "FPV ANGLE MIX", 31: "BLACKBOX ERASE", 35: "FLIP OVER AFTER CRASH (turtle)", 36: "PREARM",
    37: "BEEP GPS COUNT", 39: "VTX PIT MODE", 45: "PARALYZE", 46: "GPS RESCUE", 47: "ACRO TRAINER",
    48: "VTX CONTROL DISABLE", 49: "LAUNCH CONTROL", 52: "BEEPER MUTE",
}

SPIEGA_BLOCCHI = {
    "CLI": "la CLI è aperta (lo fa l'app adesso per leggere lo stato): normale.",
    "MSP": "il drone è collegato al computer via USB: normale, con la USB l'arm è sempre bloccato.",
    "RXLOSS": "la ricevente non riceve la radio.",
    "FAILSAFE": "il drone è in failsafe.",
    "THROTTLE": "il gas non è al minimo.",
    "ANGLE": "il drone è troppo inclinato per armare (parametro small_angle).",
    "BOOTGRACE": "la scheda si è appena accesa: aspetta qualche secondo.",
    "NOPREARM": "è impostato un interruttore PREARM che non è attivo.",
    "LOAD": "il processore è troppo carico.",
    "CMS": "il menu OSD si è aperto durante il giro degli stick (combinazione gas a metà, yaw a sinistra, "
           "pitch avanti): normale nel test, si chiude riaccendendo il drone.",
    "CALIB": "i sensori si stanno calibrando: tieni il drone fermo.",
    "NO_ACC_CAL": "l'accelerometro non è calibrato: va fatto con il drone in piano.",
    "ARMSWITCH": "l'interruttore ARM era già attivo all'accensione: riportalo su OFF.",
    "BADRX": "l'interruttore ARM era attivo quando è tornato il segnale radio: riportalo su OFF.",
    "RPMFILTER": "il filtro RPM non riceve gli RPM dagli ESC (normale senza batteria: gli ESC sono spenti).",
    "DSHOT_TELEM": "gli ESC non mandano la telemetria DShot (normale senza batteria: gli ESC sono spenti).",
    "MOTOR_PROTO": "il protocollo dei motori non è impostato.",
    "REBOOT_REQD": "serve un riavvio per applicare delle modifiche.",
    "GPS": "il GPS non ha abbastanza satelliti per armare.",
    "RESCUE SW": "l'interruttore GPS RESCUE è attivo.",
    "PARALYZE": "è attiva la modalità PARALYZE.",
    "NOGYRO": "il giroscopio non risponde: problema serio della scheda.",
    "RUNAWAY": "è scattata la protezione runaway takeoff.",
    "CRASH": "è scattata la rilevazione di crash.",
    "BOXFAILSAFE": "è attivo l'interruttore FAILSAFE.",
}


def leggi_rc(ser):
    r = msp(ser, MSP_RC)
    return list(struct.unpack(f"<{len(r) // 2}H", r)) if r else []


def blocchi_arm(ser):
    r = msp(ser, MSP_STATUS_EX)
    if not r or len(r) < 16:
        return 0
    pos = 16 + r[15]
    if len(r) < pos + 5:
        return 0
    return struct.unpack("<I", r[pos + 1:pos + 5])[0]


def leggi_modi(ser):
    r = msp(ser, MSP_MODE_RANGES) or b""
    modi = []
    for i in range(0, len(r) - 3, 4):
        box, aux, inizio, fine = r[i:i + 4]
        if fine > inizio:
            modi.append({"box": box, "nome": NOMI_MODI.get(box, f"modo n.{box}"), "aux": aux,
                         "da": 900 + inizio * 25, "a": 900 + fine * 25})
    return modi


ETICHETTE = ["Roll", "Pitch", "Yaw", "Gas"]


def riga_canali(v):
    """Riga corta (sta in una finestra stretta): R=roll P=pitch Y=yaw G=gas, poi gli AUX."""
    sigle = ["R", "P", "Y", "G"]
    parti = [f"{sigle[i]}{v[i]}" for i in range(min(len(v), 4))]
    parti += [f"A{i - 3}:{v[i]}" for i in range(4, min(len(v), 9))]
    return " ".join(parti)


PASSO = {"testo": ""}   # istruzione in corso (serve anche alle prove simulate)


PASSO["numero"] = 0


def istruisci(testo, aspetta_invio=True):
    """Mostra l'istruzione ben in evidenza. Per i passi principali aspetta che il pilota prema Invio."""
    if aspetta_invio:
        PASSO["numero"] += 1
        print("\n" + "-" * 56)
        print(f"▶ PASSO {PASSO['numero']}: {testo}")
        print("-" * 56)
        input("  Premi Invio quando sei pronto ... ")
        print("  (controllo in corso: esegui il comando e tienilo)")
    else:
        print(f"\n▶ {testo}")
    PASSO["testo"] = testo


def attendi(ser, condizione, tenuta=1.0, limite=30):
    """Legge i canali finché la condizione resta vera per 'tenuta' secondi. None se scade il tempo."""
    inizio = time.time()
    da_quando = None
    ultima = ""
    while time.time() - inizio < limite:
        v = leggi_rc(ser)
        if len(v) >= 4:
            riga = riga_canali(v)
            if riga != ultima:
                print(f"\r  {riga}   ", end="", flush=True)
                ultima = riga
            if condizione(v):
                da_quando = da_quando or time.time()
                if time.time() - da_quando >= tenuta:
                    print()
                    return v
            else:
                da_quando = None
        time.sleep(0.1)
    print()
    return None


_RX_IN_CORSO = {}


def menu_test_rx(drone):
    """Esegue il test della ricevente. Se si interrompe a metà (cavo staccato, Interrompi),
    salva comunque quello che è stato verificato fin lì, segnato come INTERROTTO."""
    _RX_IN_CORSO.clear()
    try:
        return _test_rx(drone)
    except BaseException:
        rap, momento = _RX_IN_CORSO.get("rap"), _RX_IN_CORSO.get("momento")
        if rap and momento:
            percorso = os.path.join(cartella(drone["nome"]), f"test_ricevente_{momento:%Y-%m-%d_%H-%M}.txt")
            with open(percorso, "w", encoding="utf-8") as f:
                f.write(f"Test della ricevente - {drone['nome']} - {momento:%d/%m/%Y %H:%M}\n\n")
                for livello, titolo, perche, _ in rap:
                    f.write(f"[{livello}] {titolo}\n{rientra(perche)}\n")
                f.write("\nESITO: INTERROTTO a metà: questi sono solo i passi fatti prima dell'interruzione. "
                        "Ripeti il test.\n")
            print(f"Quello che avevo già verificato è salvato in: {percorso}")
        raise


def _test_rx(drone):
    print("\nTEST DELLA RICEVENTE")
    print(" Serve: drone collegato con la USB, radio ACCESA e agganciata. Niente batteria.")
    print(" Il drone non verrà mai armato.")
    print(" Per ogni passo: leggi l'istruzione, premi Invio, poi esegui il comando e tienilo")
    print(" finché l'app non conferma. Per interrompere in qualsiasi momento: Ctrl + C.")
    if not conferma("Radio accesa e drone collegato?"):
        print("Annullato.")
        return None
    rap = []
    momento = datetime.now()
    _RX_IN_CORSO.update(rap=rap, momento=momento)

    def esito(livello, titolo, perche):
        rap.append((livello, titolo, perche, None))
        simbolo = {"OK": "✅", "ATTENZIONE": "⚠️ ", "VERIFICA": "🔍", "NOTA": "ℹ️ "}[livello]
        print(f"  {simbolo} {titolo}")

    with apri() as ser:
        # ---------------------------------------------------- 1. collegamento
        blocchi = blocchi_arm(ser)
        v = leggi_rc(ser)
        if blocchi & BIT_RXLOSS or len(v) < 4:
            esito("ATTENZIONE", "La scheda non riceve la radio",
                  capito("Senza collegamento con la radio il test non può continuare.",
                         "Controlla che la radio sia accesa, sul modello giusto e agganciata (luce della ricevente "
                         "fissa), poi ripeti il test."))
            stampa_rapporto(rap)
            return rap
        analog = msp(ser, MSP_ANALOG)
        rssi = struct.unpack("<H", analog[3:5])[0] * 100 // 1023 if analog and len(analog) >= 5 else None
        print(f"  La scheda riceve un segnale radio" + (f" (RSSI {rssi}%)" if rssi is not None else "") +
              ": ora verifico che arrivino davvero i comandi.")

        # ---------------------------------------------------- 2. posizione di riposo
        modi = leggi_modi(ser)
        arm = [m for m in modi if m["nome"] == "ARM"]
        istruisci("Stick al centro, GAS AL MINIMO e tutti gli interruttori in posizione di riposo (ARM spento).")

        def a_riposo(v):
            if any(abs(v[i] - 1500) > 60 for i in range(3)) or v[3] > 1100:
                return False
            return not any(m["da"] <= v[4 + m["aux"]] <= m["a"] for m in arm if 4 + m["aux"] < len(v))

        riposo = attendi(ser, a_riposo, tenuta=1.5, limite=60)
        if not riposo:
            esito("ATTENZIONE", "In 60 secondi la radio non è mai stata a riposo",
                  capito("Per partire mi servono stick centrati, gas al minimo e interruttore ARM spento.",
                         "Controlla gli interruttori e i trim della radio, poi ripeti il test."))
            stampa_rapporto(rap)
            return rap
        scentrati = [f"{ETICHETTE[i]} {riposo[i]}" for i in range(3) if abs(riposo[i] - 1500) > 8]
        if scentrati:
            esito("VERIFICA", f"A stick rilasciati il centro non è a 1500 ({', '.join(scentrati)})",
                  capito("Il drone tenderebbe a spostarsi da solo, come se tenessi lo stick leggermente mosso.",
                         "Di solito è un trim o un subtrim sulla radio: azzeralo e ripeti il test."))
        else:
            esito("OK", "Stick ben centrati",
                  capito("A stick rilasciati roll, pitch e yaw sono a 1500 (±8): il drone non si sposta da solo."))

        # ---------------------------------------------------- 3. mappatura e direzione
        prove = [
            ("Porta il GAS AL MASSIMO e tienilo.", 3, +1, "Gas"),
            ("Porta il ROLL tutto a DESTRA (stick destro verso destra) e tienilo.", 0, +1, "Roll"),
            ("Porta il PITCH tutto in AVANTI (stick verso l'alto) e tienilo.", 1, +1, "Pitch"),
            ("Porta lo YAW tutto a DESTRA (stick sinistro verso destra, il muso girerebbe a destra) tenendo il GAS IN BASSO, e tienilo.", 2, +1, "Yaw"),
        ]
        def prova_stick(testo, atteso, segno, nome, tentativo):
            """Un tentativo: ritorna (esito, canale, valori). esito: 'ok', 'fermo', 'canale', 'verso'.
            Conta il canale ATTESO: se si muove a fondo il passo è valido anche se si muove un po' un altro
            canale (es. gas e yaw sono sullo stesso stick). 'canale' solo se l'atteso resta fermo e un altro
            si muove a fondo per almeno 2 secondi."""
            istruisci(testo if tentativo == 1 else f"(SECONDA PROVA) {testo}")
            inizio = time.time()
            da_atteso = da_altro = None
            altro = None
            ultima = ""
            risultato = ("fermo", None, None)
            while time.time() - inizio < 30:
                v = leggi_rc(ser)
                if len(v) < 4:
                    time.sleep(0.1)
                    continue
                riga = riga_canali(v)
                if riga != ultima:
                    print(f"\r  {riga}   ", end="", flush=True)
                    ultima = riga
                if abs(v[atteso] - riposo[atteso]) > 350:
                    da_atteso = da_atteso or time.time()
                    if time.time() - da_atteso >= 1.0:
                        verso = 1 if v[atteso] > riposo[atteso] else -1
                        risultato = ("ok" if verso == segno else "verso", atteso, v)
                        break
                else:
                    da_atteso = None
                    mossi = [i for i in range(4) if i != atteso and abs(v[i] - riposo[i]) > 350]
                    if mossi:
                        da_altro = da_altro or time.time()
                        altro = max(mossi, key=lambda i: abs(v[i] - riposo[i]))
                        if time.time() - da_altro >= 2.0:
                            risultato = ("canale", altro, v)
                            break
                    else:
                        da_altro = None
                time.sleep(0.1)
            print()
            istruisci("Rilascia gli stick (gas al minimo).", aspetta_invio=False)
            attendi(ser, a_riposo, tenuta=0.8, limite=30)
            return risultato

        for testo, atteso, segno, nome in prove:
            risultato, canale, v = prova_stick(testo, atteso, segno, nome, 1)
            if risultato != "ok":
                print(f"  Risultato inatteso su {nome.lower()}: ripeto il passo per conferma (verifica doppia).")
                primo = risultato
                risultato, canale, v = prova_stick(testo, atteso, segno, nome, 2)
                if risultato == "ok":
                    esito("NOTA", f"{nome}: giusto alla seconda prova",
                          capito("Alla prima prova il risultato era sbagliato, alla seconda canale e direzione sono "
                                 "corretti: probabilmente la prima volta lo stick è andato nel verso sbagliato."))
                    continue
            if risultato == "fermo":
                esito("ATTENZIONE", f"{nome}: non ho visto nessun movimento",
                      capito("Per due volte, in 30 secondi, nessun canale si è mosso.",
                             "Ripeti il test muovendo lo stick fino in fondo e tenendolo finché l'app conferma."))
            elif risultato == "canale":
                esito("ATTENZIONE", f"Muovendo il {nome.lower()} si muove il {ETICHETTE[canale].lower()} (visto due volte)",
                      capito("L'ordine dei canali nella scheda non corrisponde a quello della radio: il drone "
                             "risponderebbe ai comandi sbagliati. Così non si vola.",
                             "In Betaflight Configurator, scheda Ricevitore, scegli l'ordine dei canali giusto per "
                             "la tua radio (per ELRS e Crossfire di solito AETR1234), salva e ripeti il test."))
            elif risultato == "verso":
                esito("ATTENZIONE", f"{nome}: va al contrario (visto due volte)",
                      capito("Il drone andrebbe nella direzione opposta al comando. Così non si vola.",
                             "Sulla radio togli l'inversione (reverse) di quel canale. Poi controlla in Betaflight "
                             "Configurator, scheda Ricevitore: con lo stick a destra o in avanti la barra deve "
                             "andare verso 2000."))
            else:
                esito("OK", f"{nome}: canale e direzione giusti",
                      capito(f"Lo stick muove il canale giusto nel verso giusto (arriva a {v[canale]})."))

        # ---------------------------------------------------- 4. corse complete
        istruisci("Muovi ENTRAMBI gli stick lungo tutto il bordo, in cerchio, per qualche giro (8 secondi).")
        minimi, massimi = [9999] * 4, [0] * 4
        inizio = time.time()
        while time.time() - inizio < 8:
            v = leggi_rc(ser)
            if len(v) >= 4:
                for i in range(4):
                    minimi[i], massimi[i] = min(minimi[i], v[i]), max(massimi[i], v[i])
                print(f"\r  {riga_canali(v)}   ", end="", flush=True)
            time.sleep(0.05)
        print()
        corte = [f"{ETICHETTE[i]} {minimi[i]}-{massimi[i]}" for i in range(4) if minimi[i] > 1020 or massimi[i] < 1980]
        if corte:
            esito("VERIFICA", f"Alcuni stick non arrivano in fondo ({', '.join(corte)})",
                  capito("Servono circa 1000 e 2000 agli estremi: senza, non hai tutta la potenza e tutta la "
                         "velocità di rotazione.",
                         "Se hai mosso gli stick fino in fondo, controlla sulla radio la corsa dei canali "
                         "(endpoint o limiti, pagina Uscite) e ripeti il test."))
        else:
            esito("OK", "Gli stick arrivano in fondo in tutte le direzioni",
                  capito("Tutti e quattro i canali vanno da circa 1000 a circa 2000 (" +
                         ", ".join(f"{ETICHETTE[i]} {minimi[i]}-{massimi[i]}" for i in range(4)) +
                         "): hai tutta la potenza e tutta la velocità di rotazione."))
        if minimi[3] >= 1050:
            esito("ATTENZIONE", f"Con il gas al minimo il canale vale {minimi[3]}",
                  capito("Sopra 1050 Betaflight pensa che il gas non sia a zero e non ti fa armare.",
                         "Sulla radio controlla trim e corsa del gas: al minimo deve arrivare intorno a 1000."))

        istruisci("Rilascia gli stick (gas al minimo).", aspetta_invio=False)
        attendi(ser, a_riposo, tenuta=0.8, limite=30)

        # ---------------------------------------------------- 5a. scopri gli interruttori
        if modi and conferma("\nVuoi scoprire quale interruttore attiva ogni modalità (consigliato)?"):
            print("\n" + "-" * 56)
            print("▶ SCOPRI GLI INTERRUTTORI")
            print("  Muovi gli interruttori uno alla volta: ti scrivo quale modalità si attiva.")
            print("  Dura 60 secondi; per finire prima premi Ctrl + C.")
            print("-" * 56)
            PASSO["testo"] = "scopri gli interruttori"
            visti = set()
            prima = None
            inizio = time.time()
            try:
                while time.time() - inizio < 60:
                    v = leggi_rc(ser)
                    attivi = tuple(f"{m['nome']} (AUX{m['aux'] + 1})" for m in modi
                                   if 4 + m["aux"] < len(v) and m["da"] <= v[4 + m["aux"]] <= m["a"])
                    if attivi != prima:
                        print(f"  Attive ora: {', '.join(attivi) if attivi else 'nessuna modalità'}")
                        visti.update(attivi)
                        prima = attivi
                    time.sleep(0.1)
            except KeyboardInterrupt:
                print("  Scoperta terminata.")
            mai = [f"{m['nome']} (AUX{m['aux'] + 1})" for m in modi
                   if f"{m['nome']} (AUX{m['aux'] + 1})" not in visti]
            print("\n  Riepilogo degli interruttori:")
            for m in modi:
                print(f"    AUX{m['aux'] + 1}: {m['nome']}  (attivo tra {m['da']} e {m['a']})")
            if mai:
                print(f"  Non le hai attivate durante la scoperta: {', '.join(mai)}.")
            print("  Suggerimento: annota sulla radio o su un'etichetta quale interruttore è il GPS RESCUE.")

        # ---------------------------------------------------- 5b. interruttori
        if not modi:
            esito("VERIFICA", "Nessuna funzione è assegnata agli interruttori",
                  capito("Senza un interruttore ARM il drone non si arma.",
                         "In Betaflight Configurator, scheda Modi, assegna almeno ARM a un interruttore."))
        elif not arm:
            esito("ATTENZIONE", "Manca l'interruttore ARM",
                  capito("Senza un interruttore ARM il drone non si arma.",
                         "In Betaflight Configurator, scheda Modi, assegna ARM a un interruttore."))
        for m in modi:
            canale = 4 + m["aux"]
            descr = f"{m['nome']} (AUX{m['aux'] + 1}, attivo tra {m['da']} e {m['a']})"
            nota = "  Il drone NON si armerà: con la USB collegata Betaflight lo impedisce." if m["nome"] == "ARM" else ""
            if m["nome"] == "BEEPER":
                nota = "  Il buzzer suonerà: è normale."
            prima = leggi_rc(ser) or []
            istruisci(f"Attiva l'interruttore di {descr}.{nota}")
            v = attendi(ser, lambda v, m=m, c=canale: c < len(v) and m["da"] <= v[c] <= m["a"], tenuta=0.7, limite=30)
            if not v:
                dopo = leggi_rc(ser) or []
                mossi = [i for i in range(4, min(len(prima), len(dopo))) if abs(dopo[i] - prima[i]) > 300]
                if mossi and canale not in mossi:
                    altro = ", ".join(f"AUX{i - 3}" for i in mossi)
                    esito("ATTENZIONE", f"{m['nome']}: hai mosso {altro}, ma Betaflight lo aspetta su AUX{m['aux'] + 1}",
                          capito(f"L'interruttore che usi per {m['nome']} manda il comando su un canale diverso da "
                                 "quello impostato nella scheda, quindi la funzione non parte.",
                                 f"In Betaflight Configurator, scheda Modi, sposta {m['nome']} su {altro} (oppure "
                                 "cambia il canale dell'interruttore nella radio), salva e ripeti il test."))
                elif mossi:
                    esito("ATTENZIONE", f"{m['nome']}: AUX{m['aux'] + 1} si muove ma non arriva tra {m['da']} e {m['a']}",
                          capito(f"Il canale giusto si muove (ora è a {dopo[canale]}), ma non entra nell'intervallo "
                                 "impostato in Betaflight, quindi la funzione non parte.",
                                 "In Betaflight Configurator, scheda Modi, allarga l'intervallo di "
                                 f"{m['nome']} finché copre la posizione dell'interruttore, salva e ripeti il test."))
                else:
                    esito("ATTENZIONE", f"{m['nome']}: in 30 secondi non si è mosso nessun interruttore",
                          capito(f"La scheda aspetta {m['nome']} su AUX{m['aux'] + 1}, tra {m['da']} e {m['a']}, "
                                 "ma non ha visto muoversi nessun canale AUX.",
                                 "Ripeti il test e tieni l'interruttore attivo finché l'app non lo conferma. Se "
                                 "ancora non si muove nulla, controlla nella radio che quell'interruttore sia "
                                 "assegnato a un canale (pagina Mixer)."))
                continue
            istruisci(f"Disattiva l'interruttore di {m['nome']}.", aspetta_invio=False)
            v2 = attendi(ser, lambda v, m=m, c=canale: not (m["da"] <= v[c] <= m["a"]), tenuta=0.7, limite=30)
            if v2:
                esito("OK", f"{m['nome']}: si attiva e si disattiva",
                      capito(f"L'interruttore su AUX{m['aux'] + 1} accende la funzione (a {v[canale]}) e la "
                             f"spegne (a {v2[canale]})."))
            else:
                esito("ATTENZIONE", f"{m['nome']}: non si spegne",
                      capito(f"Anche con l'interruttore spento, AUX{m['aux'] + 1} resta nell'intervallo che "
                             "accende la funzione.",
                             f"In Betaflight Configurator, scheda Modi, restringi l'intervallo di {m['nome']} "
                             "perché copra solo la posizione 'acceso' dell'interruttore."))

        # ---------------------------------------------------- 6. failsafe
        istruisci("Ora SPEGNI LA RADIO. (Il buzzer può suonare: è il segnale di radio persa.)")
        inizio = time.time()
        rilevato = None
        while time.time() - inizio < 30:
            b = blocchi_arm(ser)
            v = leggi_rc(ser)
            if b & BIT_RXLOSS:
                rilevato = v
                break
            print(f"\r  In attesa del failsafe ... {30 - (time.time() - inizio):3.0f} s   ", end="", flush=True)
            time.sleep(0.2)
        print()
        if rilevato is None:
            esito("ATTENZIONE", "Spenta la radio, la scheda non si accorge di aver perso il segnale",
                  capito("Se la radio era davvero spenta, la ricevente continua a mandare l'ultimo comando: in volo, "
                         "perdendo il segnale, il drone continuerebbe ad andare invece di attivare il failsafe. "
                         "È pericoloso.",
                         "Nelle impostazioni della ricevente imposta il failsafe su 'No Pulses' (nessun segnale), "
                         "poi ripeti il test."))
        else:
            gas = rilevato[3] if len(rilevato) > 3 else None
            esito("OK", "Spenta la radio, la scheda si accorge subito della perdita del segnale",
                  capito("In volo partirebbe il failsafe" + (f" (il gas va a {gas})" if gas else "") +
                         ": cosa fa il drone in quel caso lo trovi nel controllo della configurazione."))
            istruisci("RIACCENDI LA RADIO e aspetta che si riagganci (qualche secondo).", aspetta_invio=False)
            inizio = time.time()
            tornato = False
            while time.time() - inizio < 60:
                if not blocchi_arm(ser) & BIT_RXLOSS:
                    tornato = True
                    break
                print(f"\r  In attesa del segnale ... {60 - (time.time() - inizio):3.0f} s   ", end="", flush=True)
                time.sleep(0.3)
            print()
            if tornato:
                esito("OK", f"Riaccesa la radio, il segnale torna in {time.time() - inizio:.0f} s",
                      capito("La ricevente si riaggancia da sola dopo una perdita del segnale."))
            else:
                esito("ATTENZIONE", "Riaccesa la radio, il segnale non torna",
                      capito("Dopo 60 secondi la ricevente non si è riagganciata.",
                             "Controlla che la radio sia sul modello giusto e che il collegamento (binding) sia "
                             "fatto; se serve, rifai il binding e ripeti il test."))

    # ---------------------------------------------------- 7. perché non si arma
    print("\nControllo i blocchi all'arm ...")
    istruisci("Lascia la radio accesa, interruttori a riposo e gas al minimo.", aspetta_invio=False)
    time.sleep(3)
    with CLI() as cli:
        stato = cli.comando("status")
    riga = next((r for r in stato if "Arming disable flags" in r), "")
    nomi = riga.split(":", 1)[1].split() if ":" in riga else []
    usb = {"CLI", "MSP"}
    senza_batteria = {"DSHOT_TELEM", "RPMFILTER"}   # con la sola USB gli ESC sono spenti
    dal_test = {"CMS"}   # menu OSD aperto dal giro degli stick durante il test: si chiude riaccendendo il drone
    altri = [n for n in nomi if n not in usb | senza_batteria | dal_test]
    if not riga:
        esito("VERIFICA", "Non sono riuscito a leggere se il drone si arma",
              capito("La scheda non ha risposto quando ho chiesto i motivi che bloccano l'arm.",
                     "Ripeti il test; se si ripete, guarda la riga dei blocchi in Betaflight Configurator."))
    elif altri:
        esito("ATTENZIONE", "Il drone non si armerebbe",
              capito("Betaflight blocca l'arm per questi motivi: " +
                     "; ".join(f"{n} = {SPIEGA_BLOCCHI.get(n, 'motivo che l app non conosce ancora: cercalo nella documentazione di Betaflight')}" for n in altri),
                     "Risolvi i motivi elencati e ripeti il test."))
    else:
        spiegati = [n for n in nomi if n in usb | senza_batteria | dal_test]
        esito("OK", "Con la batteria e senza cavo USB il drone si armerebbe",
              capito("Gli unici blocchi presenti sono quelli normali durante il test" +
                     (": " + "; ".join(f"{n} = {SPIEGA_BLOCCHI[n].rstrip('.')}" for n in spiegati) if spiegati else "") +
                     "."))

    if not any(r[0] == "OK" and (": canale e direzione corretti" in r[1] or ": si attiva e si disattiva" in r[1]) for r in rap):
        esito("ATTENZIONE", "Durante il test non è arrivato nessun comando",
              capito("In nessun passo i canali si sono mossi: o i comandi non sono stati dati, oppure la scheda "
                     "riceve un segnale ma non i comandi (radio su un altro modello, problema sulla porta della "
                     "ricevente).",
                     "Ripeti il test. Prima di volare controlla in Betaflight Configurator, scheda Ricevitore, che "
                     "le barre si muovano con gli stick."))

    print("\n" + "=" * 56)
    print("  RISULTATO DEL TEST DELLA RICEVENTE")
    print("=" * 56)
    stampa_rapporto(rap)
    gravi = [r for r in rap if r[0] == "ATTENZIONE"]
    print("\n" + ("❌ TEST NON SUPERATO: risolvi i punti da controllare prima di volare."
                  if gravi else "✅ TEST SUPERATO: la ricevente risponde correttamente."))
    percorso = os.path.join(cartella(drone["nome"]), f"test_ricevente_{momento:%Y-%m-%d_%H-%M}.txt")
    with open(percorso, "w", encoding="utf-8") as f:
        f.write(f"Test della ricevente - {drone['nome']} - {momento:%d/%m/%Y %H:%M}\n\n")
        for livello, titolo, perche, _ in rap:
            f.write(f"[{livello}] {titolo}\n{rientra(perche)}\n")
        f.write("\nESITO: " + ("NON SUPERATO" if gravi else "SUPERATO") + "\n")
    print(f"Rapporto salvato in: {percorso}")
    salva_rapporto(drone, "ricevente", rap)
    return rap


# ============================================================== controllo in officina

def ultimo_test_superato(drone, prefisso):
    """Data dell'ultimo rapporto con ESITO: SUPERATO (es. prefisso 'test_ricevente_')."""
    cart = cartella(drone["nome"])
    superati = []
    for f in os.listdir(cart):
        if f.startswith(prefisso) and f.endswith(".txt"):
            percorso = os.path.join(cart, f)
            if "ESITO: SUPERATO" in open(percorso, encoding="utf-8").read():
                superati.append(datetime.fromtimestamp(os.path.getmtime(percorso)))
    return max(superati) if superati else None


CHECKLIST_CAMPO = [
    "Tensione della batteria e controllo cella per cella con il checker (4.20 V per cella)",
    "Controllo visivo del drone",
    "Eliche serrate e montate nel loro senso di rotazione",
    "Motori che girano liberi, senza rumori, sabbia o residui ferrosi",
    "Batteria fissata bene con la cinghia, il più possibile bilanciata",
    "Radio accesa, carica controllata, tutti gli switch a zero",
    "Antenne del drone (radio e video) e della radio",
    "Visori indossati e carica controllata",
    "Batteria attaccata: ascolta la musichetta di avvio",
    "GPS: Satelliti: aspetta il fix con almeno il numero richiesto dal GPS Rescue",
    "GPS: Dopo l'arm: freccia e distanza da casa sull'OSD",
    "Area sicura: persone e cose",
]


def checklist_campo(drone):
    """La checklist da campo di questo drone: senza GPS spariscono i passi dei satelliti."""
    gps = ha_gps(drone)
    prof = profilo_batteria(leggi_componenti(drone["nome"]))
    voci = [v[5:] if v.startswith("GPS: ") else v for v in CHECKLIST_CAMPO if gps or not v.startswith("GPS: ")]
    return [v.replace("(4.20 V per cella)", f"({volt(prof['piena'])} V per cella, {prof['nome']})") for v in voci]


def menu_officina(drone):
    momento = datetime.now()
    try:
        import fpv_banco as banco   # prove al banco: se il file manca, l'officina fa i passi di sempre
    except Exception:   # noqa: BLE001
        banco = None
    elenco = [("CONFIGURAZIONE", "Configurazione", "solo USB")]
    if banco:
        elenco.append(("ASSETTO DELLA SCHEDA", "Assetto della scheda", "solo USB, muovi il drone in mano"))
    elenco += [("RICEVENTE", "Ricevente", "radio accesa, niente batteria"),
               ("ESC E BATTERIA", "ESC e batteria", "batteria attaccata, SENZA eliche")]
    if banco:
        elenco += [("MOTORI: ORDINE E VERSO", "Motori: ordine e verso", "batteria attaccata, SENZA eliche"),
                   ("VIBRAZIONI DEI MOTORI", "Vibrazioni dei motori", "batteria attaccata, SENZA eliche")]
    totale = len(elenco)
    print("\n" + "=" * 56)
    print("  CONTROLLO IN OFFICINA")
    print("=" * 56)
    print(f" {totale} passi al banco, prima di andare in campo:")
    for i, (_, nome, cosa) in enumerate(elenco, 1):
        print(f"   {i}. {nome:<24} ({cosa})")
    print(" Le prove dopo la configurazione puoi saltarle una per una.")
    print(" Alla fine: riepilogo e registro salvato con data e ora.")
    if not conferma("Iniziamo?"):
        return None
    passi = []   # (nome, esito, rapporto)

    def esito_di(rap):
        if rap is None:
            return "SALTATO"
        return "NON SUPERATO" if any(r[0] == "ATTENZIONE" for r in rap) else "SUPERATO"

    def titolo(i):
        print("\n" + "#" * 56 + f"\n# PASSO {i} DI {totale}: {elenco[i - 1][0]}\n" + "#" * 56)

    for i, (_, nome, _) in enumerate(elenco, 1):
        titolo(i)
        rap = None
        if nome == "Configurazione":
            rap = menu_configurazione(drone)
        elif nome == "Assetto della scheda":
            if conferma("Controllo l'assetto della scheda? (solo USB, ti chiedo di muovere il drone)"):
                rap = banco.menu_assetto(drone)
        elif nome == "Ricevente":
            ultimo = ultimo_test_superato(drone, "test_ricevente_")
            if ultimo:
                giorni = (momento - ultimo).days
                print(f" Ultimo test della ricevente superato: {ultimo:%d/%m/%Y} ({'oggi' if giorni <= 0 else 'ieri' if giorni == 1 else f'{giorni} giorni fa'}).")
            else:
                print(" Questo drone non ha ancora un test della ricevente superato: è fortemente consigliato.")
            if conferma("Eseguo il test della ricevente? (accendi la radio)"):
                rap = menu_test_rx(drone)
        elif nome == "ESC e batteria":
            rap = menu_test_esc(drone)
        elif nome == "Motori: ordine e verso":
            if conferma("Controllo ordine e verso dei motori? (batteria attaccata, SENZA eliche)"):
                rap = banco.menu_motori(drone)
        elif nome == "Vibrazioni dei motori":
            if conferma("Controllo le vibrazioni dei motori? (batteria attaccata, SENZA eliche)"):
                rap = banco.menu_vibrazioni(drone)
        passi.append((nome, esito_di(rap), rap))

    # --- riepilogo
    print("\n" + "=" * 56)
    print("  RIEPILOGO CONTROLLO IN OFFICINA")
    print("=" * 56)
    simboli = {"SUPERATO": "✅", "NON SUPERATO": "❌", "SALTATO": "⏭️ "}
    for nome, esito, _ in passi:
        print(f"  {simboli[esito]} {nome}: {esito}")
    problemi = [(nome, r) for nome, _, rap in passi if rap for r in rap if r[0] == "ATTENZIONE"]
    saltati = [nome for nome, esito, _ in passi if esito == "SALTATO"]
    if problemi:
        print("\n❌ NON PRONTO PER IL CAMPO. Da risolvere:")
        for nome, r in problemi:
            print(f"   - [{nome}] {r[1]}")
    elif saltati:
        print(f"\n⚠️  PRONTO CON RISERVA: passi saltati ({', '.join(saltati)}).")
    else:
        print("\n✅ PRONTO PER IL CAMPO.")
    print("\nIn campo, la tua checklist:")
    for i, voce in enumerate(checklist_campo(drone), 1):
        print(f"  {i:>2}. [ ] {voce}")

    percorso = os.path.join(cartella(drone["nome"]), f"officina_{momento:%Y-%m-%d_%H-%M}.txt")
    with open(percorso, "w", encoding="utf-8") as f:
        f.write(f"Controllo in officina - {drone['nome']} - {momento:%d/%m/%Y %H:%M}\n\n")
        for nome, esito, rap in passi:
            f.write(f"== {nome}: {esito}\n")
            for livello, titolo, perche, _ in (rap or []):
                if livello != "OK":
                    f.write(f"   [{livello}] {titolo}\n{rientra(perche, '       ')}\n")
        f.write("\nESITO: " + ("NON PRONTO" if problemi else "PRONTO CON RISERVA" if saltati else "PRONTO") + "\n")
    print(f"\nRegistro salvato in: {percorso}")
    return None


# ============================================================== menu principale

def intestazione(drone):
    print("\n" + "=" * 56)
    print("  FPV DOCTOR  ·  anteprima")
    print("=" * 56)
    if drone:
        print(f"  Drone: {drone['nome']}  ({drone['scheda']}, Betaflight {drone['versione']})")
    else:
        print("  Nessun drone collegato")
    print("-" * 56)


def scegli_drone_offline():
    elenco = sorted(set(leggi_elenco().values()))
    if not elenco:
        return None
    print("Droni conosciuti:")
    for i, n in enumerate(elenco, 1):
        print(f"  {i}. {n}")
    s = chiedi("Quale? (Invio per nessuno): ")
    if s.isdigit() and 0 < int(s) <= len(elenco):
        return {"nome": elenco[int(s) - 1], "scheda": "non collegato", "versione": "-"}
    return None


def main():
    drone = None
    print("FPV Doctor: cerco il drone collegato ...")
    if trova_porta():
        try:
            drone = riconosci()
        except Exception as e:
            print("Non riesco a riconoscere il drone:", e)
    voci = {
        "1": ("CONTROLLO IN OFFICINA (procedura completa)", menu_officina, True),
        "2": ("Configurazione", menu_configurazione, True),
        "3": ("Ricevente (radio accesa, niente batteria)", menu_test_rx, True),
        "4": ("ESC e batteria (batteria attaccata, SENZA eliche)", menu_test_esc, True),
        "5": ("Scarica il blackbox", menu_scarica, True),
        "6": ("Analizza un volo (blackbox)", menu_analisi, False),
        "7": ("Cancella i log dal drone", menu_cancella, True),
        "8": ("Correggi e migliora (cosa non va e come sistemarlo)", menu_correggi, False),
        "r": ("Riferimenti Betaflight (preset di droni simili)", menu_riferimenti, False),
    }
    sezioni = {"1": "", "2": "  -- singoli controlli --", "5": "  -- dopo il volo --", "8": "  -- modifiche --"}
    while True:
        intestazione(drone)
        for k, (testo, _, _) in voci.items():
            if sezioni.get(k):
                print(sezioni[k])
            print(f"  {k}. {testo}")
        print("  ------------------------")
        print("  9. Ricollega / cambia drone")
        print("  0. Esci")
        s = chiedi("\nScelta: ")
        if s == "0":
            print("Ciao!")
            return
        if s == "9":
            drone = None
            print("Cerco il drone collegato ...")
            if trova_porta(attesa=3):
                try:
                    drone = riconosci()
                except Exception as e:
                    print("Non riesco a riconoscere il drone:", e)
            if not drone:
                drone = scegli_drone_offline()
            continue
        s = s.lower()
        if s not in voci:
            continue
        testo, funzione, serve_usb = voci[s]
        if not drone:
            drone = scegli_drone_offline() if not serve_usb else None
            if not drone:
                print("Collega un drone e scegli 9." if serve_usb else "Nessun drone scelto.")
                continue
        if serve_usb and drone["scheda"] == "non collegato":
            print("Questa funzione richiede il drone collegato: collegalo e scegli 9.")
            continue
        try:
            funzione(drone)
        except KeyboardInterrupt:
            print("\nInterrotto. Nessuna modifica fatta.")
        except Exception as e:
            print("\nQualcosa è andato storto:", e)
            print("Copia questo messaggio e mandalo a Claude.")
        chiedi("\nPremi Invio per tornare al menu ...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCiao!")
