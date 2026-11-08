"""
FPV Doctor - prove al banco ispirate alle schede di Betaflight Configurator.

  1. Motori: ordine e verso         (scheda Motori di Configurator)
  2. Assetto della scheda           (scheda Impostazioni: il modellino che si muove, calibrazione accelerometro)
  3. Vibrazioni dei motori          (non esiste in Configurator: confronto motore per motore)

È un file a parte: se qualcosa qui non funziona, il resto dell'app resta intatto.
Regole: i motori girano solo SENZA ELICHE, dopo conferma, piano, e si fermano sempre alla fine
(anche se premi Interrompi o qualcosa va storto).
"""

import os
import re
import struct
import threading
import time
from datetime import datetime

import fpv_app as A

MSP_STATUS = 101
MSP_RAW_IMU = 102
MSP_MOTOR = 104
MSP_ATTITUDE = 108
MSP_BOARD_ALIGNMENT = 38
MSP_MIXER_CONFIG = 42
MSP_ACC_CALIBRATION = 205
MSP_SET_MOTOR = 214

A.FONTI.update({"motori": "Ordine e verso dei motori", "assetto": "Assetto della scheda",
                "vibrazioni": "Vibrazioni dei motori"})

# Quad X di Betaflight: numero del motore -> posizione, vista dall'alto con la telecamera in avanti
POSIZIONI = {1: "dietro a destra", 2: "davanti a destra", 3: "dietro a sinistra", 4: "davanti a sinistra"}
# verso di rotazione atteso (visto dall'alto) con le eliche "verso l'interno" (impostazione normale)
VERSO_NORMALE = {1: "orario", 2: "antiorario", 3: "antiorario", 4: "orario"}
INVERSO = {"orario": "antiorario", "antiorario": "orario"}

GIRO_LENTO = 1070      # valore 1000-2000: appena sopra lo stop, basta per vedere il motore muoversi
GIRO_PROVA = 1250      # per la prova vibrazioni (senza eliche è un regime moderato)


# ============================================================== motori: sicurezza prima di tutto

def _scrivi_motori(ser, valori):
    v = list(valori) + [1000] * (8 - len(valori))
    A.msp(ser, MSP_SET_MOTOR, struct.pack("<8H", *v[:8]), attesa=1)


def ferma_tutto(ser=None):
    """Ferma tutti i motori. Si può chiamare sempre: se la porta non c'è, non fa nulla."""
    try:
        if ser is not None:
            for _ in range(3):
                _scrivi_motori(ser, [1000] * 8)
            return True
        with A.apri() as s:
            for _ in range(3):
                _scrivi_motori(s, [1000] * 8)
        return True
    except Exception:   # noqa: BLE001
        return False


class Gira:
    """Fa girare UN motore (o tutti fermi) e continua a ripetere il comando finché non esci dal blocco.
    All'uscita, in ogni caso, ferma tutti i motori."""

    def __init__(self, ser, motore, valore, quanti=4):
        self.ser, self.motore, self.valore, self.quanti = ser, motore, valore, quanti
        self.attivo = threading.Event()
        self.lucchetto = threading.Lock()

    def valori(self):
        return [self.valore if i + 1 == self.motore else 1000 for i in range(self.quanti)]

    def _ripeti(self):
        while self.attivo.is_set():
            with self.lucchetto:
                try:
                    _scrivi_motori(self.ser, self.valori())
                except Exception:   # noqa: BLE001
                    pass
            time.sleep(0.3)

    def msp(self, comando, payload=b""):
        with self.lucchetto:
            return A.msp(self.ser, comando, payload)

    def __enter__(self):
        with self.lucchetto:
            _scrivi_motori(self.ser, self.valori())
        self.attivo.set()
        self.filo = threading.Thread(target=self._ripeti, daemon=True)
        self.filo.start()
        return self

    def __exit__(self, *errore):
        self.attivo.clear()
        self.filo.join(timeout=2)
        with self.lucchetto:
            ferma_tutto(self.ser)
        return False


def mostra_motore(n):
    """Riga 'in tempo reale' per la finestra: disegna il drone con il motore che sta girando."""
    print(f"\rMOTORE {n}   ", end="", flush=True)


def numero_motori(ser):
    r = A.msp(ser, MSP_MOTOR)
    valori = struct.unpack(f"<{len(r) // 2}H", r) if r else ()
    return sum(1 for v in valori if v > 0) or 4


def tipo_mixer(ser):
    r = A.msp(ser, MSP_MIXER_CONFIG)
    return r[0] if r else 3   # 3 = Quad X


def batteria_ok(drone, rap):
    """Controlli prima di attaccare la batteria (celle) e dopo (tensione). False = non far girare nulla."""
    comp = A.leggi_componenti(drone["nome"])
    celle = comp.get("celle_batteria", "")
    for chiave, nome in (("esc_celle_max", "gli ESC"), ("fc_celle_max", "la scheda di volo")):
        mx = comp.get(chiave, "")
        if celle.isdigit() and mx.isdigit() and int(celle) > int(mx):
            print(f"\n❌ NON ATTACCARE LA BATTERIA: il drone usa una {celle}S, ma {nome} reggono al massimo {mx}S.")
            rap.append(("ATTENZIONE", f"Prova fermata: batteria {celle}S, ma {nome} reggono al massimo {mx}S",
                        A.capito("Una tensione più alta di quella per cui sono costruiti li brucia appena attacchi "
                                 "la batteria.", "Correggi i Componenti se il valore è sbagliato."), None))
            return False
    return True


def tensione_ok(ser, drone, rap):
    comp = A.leggi_componenti(drone["nome"])
    celle = comp.get("celle_batteria", "")
    prof = A.profilo_batteria(comp)
    analog = A.msp(ser, A.MSP_ANALOG)
    tensione = struct.unpack("<H", analog[7:9])[0] / 100 if analog and len(analog) >= 9 else 0
    if tensione < 3:
        print("\n❌ NON VEDO LA BATTERIA: la scheda legge meno di 3 V.")
        print("   Senza batteria i motori non possono girare. Attaccala e ripeti la prova.")
        rap.append(("VERIFICA", "Prova non fatta: batteria non attaccata",
                    A.capito("I motori prendono corrente dalla batteria, non dal cavo USB.",
                             "Attacca la batteria (senza eliche) e ripeti la prova."), None))
        return False
    if celle.isdigit() and tensione / int(celle) > prof["anomala_sopra"]:
        print(f"\n❌ STACCA LA BATTERIA: {A.volt(tensione)} V sono troppi per {celle} celle {prof['nome']}.")
        rap.append(("ATTENZIONE", f"Batteria sbagliata: {tensione:.2f} V su un drone da {celle} celle",
                    A.capito("La tensione è oltre quella di una batteria piena di questo tipo: o ha più celle o è "
                             "di un altro tipo.", "Usa la batteria giusta, poi ripeti la prova."), None))
        return False
    print(f"Batteria: {A.volt(tensione)} V.")
    return True


def chiedi_scelta(domanda, opzioni, live=None):
    """Stampa le opzioni numerate (la finestra le mostra come pulsanti) e restituisce il numero scelto.
    'live' è il disegno da lasciare visibile mentre aspetto la risposta (va stampato per ultimo)."""
    print()
    for i, o in enumerate(opzioni, 1):
        print(f"  {i}. {o}")
    if live:
        print(f"\r{live}   ", end="", flush=True)
    r = A.chiedi(domanda + " ")
    if not r:
        raise Annullato()
    return int(r) if r.isdigit() and 1 <= int(r) <= len(opzioni) else None


class Annullato(Exception):
    """Il pilota ha premuto Annulla durante una prova con i motori."""


# ============================================================== 1. ordine e verso dei motori

def leggi_risorse_motori(cli):
    pin = {}
    for r in cli.comando("resource"):
        m = re.match(r"\s*resource\s+MOTOR\s+(\d+)\s+(\S+)", r)
        if m:
            pin[int(m.group(1))] = m.group(2)
    return pin


def gira_e_chiedi(ser, n, valore):
    """Fa girare il motore n e chiede dove si trova e in che verso gira. (posizione, verso) oppure (None, None)."""
    with Gira(ser, n, valore):
        A.istruisci("Ora gira piano UN motore. Guarda il drone dall'alto, con la telecamera in avanti.", False)
        p = chiedi_scelta("Quale motore sta girando?",
                          ["Dietro a destra", "Davanti a destra", "Dietro a sinistra", "Davanti a sinistra",
                           "Nessuno si muove"], live="MOTORE ?")
        if p is None or p == 5:
            return None, None
        v = chiedi_scelta("In che verso gira, guardandolo dall'alto? (aiuta un pezzetto di nastro adesivo sulla campana)",
                          ["Orario (come le lancette)", "Antiorario", "Non riesco a capirlo"], live=f"MOTORE {p}")
    print()
    return p, {1: "orario", 2: "antiorario"}.get(v)


def menu_motori(drone):
    print("\nORDINE E VERSO DEI MOTORI")
    print(" Come la scheda Motori di Betaflight Configurator, ma guidata: faccio girare piano un motore alla volta")
    print(" e tu mi dici quale si muove e in che verso. Un ordine sbagliato fa ribaltare il drone al decollo.")
    rap = []
    momento = datetime.now()
    if not batteria_ok(drone, rap):
        A.stampa_rapporto(rap)
        A.salva_rapporto(drone, "motori", rap)
        return rap
    print("\n 1. SMONTA TUTTE LE ELICHE. Qui i motori girano davvero.")
    print(" 2. Spegni la radio (o lascia il drone disarmato) e lascia il cavo USB collegato.")
    print(" 3. Attacca la batteria e appoggia il drone sul tavolo, telecamera in avanti, lontano dalle mani.")
    print(" Se qualcosa non ti convince: premi Interrompi, oppure stacca la batteria.")
    if not A.conferma("Eliche SMONTATE, batteria attaccata, drone fermo sul tavolo?"):
        print("Annullato: nessun motore ha girato.")
        return None

    risultati = {}   # uscita della scheda -> (posizione, verso)
    with A.apri() as ser:
        try:
            if tipo_mixer(ser) != 3:
                print("Questa prova è pensata per i quadricotteri a X: il tuo drone usa un'altra configurazione.")
                rap.append(("NOTA", "Prova non fatta: il drone non è un quadricottero a X",
                            A.capito("Ordine e verso dipendono dalla forma del drone.",
                                     "Usa la scheda Motori di Betaflight Configurator."), None))
                A.stampa_rapporto(rap)
                return rap
            if not tensione_ok(ser, drone, rap):
                A.stampa_rapporto(rap)
                A.salva_rapporto(drone, "motori", rap)
                return rap
            ferma_tutto(ser)
            for n in range(1, 5):
                print(f"\n--- Motore {n} di 4 ---")
                valore = GIRO_LENTO
                pos, verso = gira_e_chiedi(ser, n, valore)
                if pos is None:
                    print("Lo faccio girare un po' più forte ...")
                    pos, verso = gira_e_chiedi(ser, n, valore + 50)
                risultati[n] = (pos, verso)
        except Annullato:
            ferma_tutto(ser)
            print("\nProva annullata: motori fermi. Nessuna modifica al drone.")
            return None
        finally:
            ferma_tutto(ser)
            print("\nMotori fermi. Puoi staccare la batteria.\n")

    # --- giudizio
    print("Quello che hai visto:")
    for n, (pos, verso) in risultati.items():
        dove = POSIZIONI.get(pos, "nessuno si è mosso")
        print(f"  Uscita {n}: {dove}{', ' + verso if verso else ''}")
    fermi = [n for n, (p, _) in risultati.items() if p is None]
    posizioni = [p for p, _ in risultati.values() if p]
    ordine_giusto = all(p == n for n, (p, _) in risultati.items())
    correzione = None
    if fermi:
        rap.append(("ATTENZIONE", f"Motori che non girano: {', '.join(str(n) for n in fermi)}",
                    A.capito("La scheda manda il comando ma quel motore non si muove: saldatura del filo segnale, "
                             "ESC danneggiato o motore bloccato.",
                             "Controlla saldature e fili di quel motore, poi ripeti la prova. Non volare così."), None))
    elif len(set(posizioni)) < len(posizioni):
        rap.append(("VERIFICA", "Due uscite sembrano muovere lo stesso motore",
                    A.capito("Probabilmente una risposta è stata data di fretta: ogni uscita comanda un motore diverso.",
                             "Ripeti la prova con calma."), None))
    elif ordine_giusto:
        rap.append(("OK", "Ordine dei motori giusto",
                    A.capito("Ogni motore è al posto che Betaflight si aspetta (1 dietro a destra, 2 davanti a "
                             "destra, 3 dietro a sinistra, 4 davanti a sinistra)."), None))
    else:
        sbagliati = [f"uscita {n} → {POSIZIONI[p]} (dovrebbe essere {POSIZIONI[n]})"
                     for n, (p, _) in risultati.items() if p != n]
        rap.append(("ATTENZIONE", "Ordine dei motori sbagliato",
                    A.capito("Betaflight comanda un motore pensando che sia in un altro punto: appena dai gas il "
                             "drone si ribalta o gira su se stesso. " + "; ".join(sbagliati) + ".",
                             "Posso correggerlo io rimappando le uscite della scheda (salvo prima una copia)."), None))
        correzione = {n: p for n, (p, _) in risultati.items()}

    # verso: confronto con l'impostazione eliche della scheda (yaw_motors_reversed)
    invertito = None
    verso_da_correggere = None
    try:
        with A.CLI() as cli:
            invertito = A.valore_letto(A.get_righe(cli, "yaw_motors_reversed"), "yaw_motors_reversed")
    except Exception:   # noqa: BLE001
        pass
    eliche_fuori = str(invertito).upper() == "ON"
    atteso = {p: (INVERSO[v] if eliche_fuori else v) for p, v in VERSO_NORMALE.items()}
    versi = {p: v for p, v in risultati.values() if p and v}
    if versi and not fermi:
        sbagliati = [p for p, v in versi.items() if v != atteso[p]]
        nome_eliche = "eliche verso l'esterno (props out)" if eliche_fuori else "eliche verso l'interno (props in)"
        if not sbagliati:
            rap.append(("OK", "Verso di rotazione giusto",
                        A.capito(f"Tutti i motori girano come serve con l'impostazione {nome_eliche}.",
                                 "Monta le eliche seguendo il verso di ogni motore."), None))
        elif len(sbagliati) == len(versi) == 4:
            nuovo = "OFF" if eliche_fuori else "ON"
            verso_da_correggere = (nuovo, str(invertito))
            rap.append(("ATTENZIONE", "Tutti i motori girano al contrario rispetto all'impostazione",
                        A.capito(f"La scheda è impostata per {nome_eliche}, ma i motori girano tutti nell'altro "
                                 "verso. In volo lo yaw sarebbe al contrario e il drone potrebbe non decollare.",
                                 f"La soluzione più semplice: dire a Betaflight che le eliche sono montate al "
                                 f"contrario (yaw_motors_reversed = {nuovo}). Posso farlo io."), None))

        else:
            nomi = ", ".join(POSIZIONI[p] for p in sbagliati)
            rap.append(("ATTENZIONE", f"Motori che girano al contrario: {nomi}",
                        A.capito("Quei motori girano nel verso opposto agli altri della loro diagonale: con le "
                                 "eliche montate spingerebbero nel verso sbagliato.",
                                 "Il verso si inverte nell'ESC: in Betaflight Configurator, scheda Motori, pulsante "
                                 "'Direzione motori'; oppure con ESC Configurator (esc-configurator.com) per Bluejay "
                                 "e BLHeli. Poi ripeti questa prova."), None))
    elif not fermi:
        rap.append(("NOTA", "Verso di rotazione non controllato",
                    A.capito("Non sei riuscito a vedere il verso: senza eliche a volte è difficile.",
                             "Metti un pezzetto di nastro adesivo sulla campana di ogni motore e ripeti la prova."),
                    None))

    A.stampa_rapporto(rap)
    A.salva_rapporto(drone, "motori", rap)
    salva_testo(drone, "motori", momento, rap)
    if correzione and A.conferma("Correggo l'ordine dei motori rimappando le uscite (con copia di sicurezza)?"):
        rimappa_motori(drone, correzione)
    elif verso_da_correggere:
        print("\nCORREZIONE DEL VERSO: dico a Betaflight in che verso girano davvero i motori.")
        A.applica_modifica(drone, "yaw_motors_reversed",
                           {"consigliato": verso_da_correggere[0], "attuale": verso_da_correggere[1], "al_banco": True,
                            "prova": ["Monta le eliche seguendo il verso di rotazione di ogni motore",
                                      "Ripeti 'Motori: ordine e verso' per conferma",
                                      "Primo decollo piano, in un posto aperto"]})
    return rap


def rimappa_motori(drone, mappa):
    """mappa: uscita n -> posizione p dove si trova davvero il motore.
    Il pin che oggi è 'MOTOR n' deve diventare 'MOTOR p'."""
    momento = datetime.now()
    with A.CLI() as cli:
        print("Salvo una copia della configurazione attuale ...")
        backup = cli.comando("diff all", attesa=20)
        percorso = os.path.join(A.cartella(drone["nome"]), f"backup_{momento:%Y-%m-%d_%H-%M-%S}.txt")
        open(percorso, "w", encoding="utf-8").write("\n".join(backup) + "\n")
        print(f"Copia salvata in: {percorso}")
        prima = leggi_risorse_motori(cli)
        if not all(n in prima for n in range(1, 5)):
            print("Non riesco a leggere i pin dei motori: nessuna modifica. Usa la scheda Motori di Configurator.")
            return False
        nuovi = {mappa[n]: prima[n] for n in range(1, 5)}
        print("Pin dei motori, prima → dopo:")
        for p in range(1, 5):
            print(f"  MOTOR {p}: {prima[p]} → {nuovi[p]}")
        for n in range(1, 5):
            cli.comando(f"resource MOTOR {n} NONE")
        errori = []
        for p in range(1, 5):
            errori += [r for r in cli.comando(f"resource MOTOR {p} {nuovi[p]}") if "ERROR" in r.upper()]
        letti = leggi_risorse_motori(cli)
        if errori or any(letti.get(p, "").upper() != nuovi[p].upper() for p in range(1, 5)):
            print("❌ Verifica 1 fallita: la scheda non ha accettato la nuova mappa. NON salvo nulla:")
            print("   al riavvio torna tutto come prima.")
            return False
        print("✅ Verifica 1 (subito): la scheda conferma la nuova mappa")
        cli.salva = True
        print("Salvo sul drone (la scheda si riavvia) ...")
    with A.CLI() as cli:
        dopo = leggi_risorse_motori(cli)
    ok = all(dopo.get(p, "").upper() == nuovi[p].upper() for p in range(1, 5))
    print("✅ Verifica 2 (dopo il riavvio): la mappa è rimasta salvata" if ok else
          f"❌ Verifica 2 fallita: dopo il riavvio la mappa non è quella scritta. Copia di prima: {percorso}")
    with open(os.path.join(A.cartella(drone["nome"]), "registro_modifiche.txt"), "a", encoding="utf-8") as f:
        f.write(f"{momento:%d/%m/%Y %H:%M}  ordine motori: {prima} -> {nuovi}  "
                f"verifica2={'OK' if ok else 'FALLITA'}  backup={os.path.basename(percorso)}\n")
    if ok:
        print("\nORA LA PROVA CHE CONTA: ripeti 'Ordine e verso dei motori' (sempre senza eliche).")
        print(f"Se qualcosa non torna, la copia di prima è: {percorso}")
    return ok


# ============================================================== 2. assetto della scheda

def assetto(ser):
    r = A.msp(ser, MSP_ATTITUDE)
    if not r or len(r) < 6:
        return None
    roll, pitch, yaw = struct.unpack("<hhh", r[:6])
    return roll / 10, pitch / 10, yaw


def allineamento(ser):
    r = A.msp(ser, MSP_BOARD_ALIGNMENT)
    return struct.unpack("<hhh", r[:6]) if r and len(r) >= 6 else (0, 0, 0)


def ha_accelerometro(ser):
    r = A.msp(ser, MSP_STATUS)
    if not r or len(r) < 6:
        return True
    return bool(struct.unpack("<H", r[4:6])[0] & 1)


def segui(ser, condizione, tenuta=1.0, limite=40):
    """Mostra il drone che si muove sullo schermo finché la condizione resta vera per 'tenuta' secondi."""
    inizio, da = time.time(), None
    ultimo = None
    while time.time() - inizio < limite:
        a = assetto(ser)
        if a:
            ultimo = a
            print(f"\rASSETTO {a[0]:.1f} {a[1]:.1f} {a[2]}   ", end="", flush=True)
            if condizione(a):
                da = da or time.time()
                if time.time() - da >= tenuta:
                    print()
                    return a
            else:
                da = None
        time.sleep(0.08)
    print()
    return None if ultimo is None or not condizione(ultimo) else ultimo


def gradi(x):
    return f"{x:.1f}".replace(".", ",") + "°"


def differenza_angolo(a, b):
    return (a - b + 180) % 360 - 180


def menu_assetto(drone):
    print("\nASSETTO DELLA SCHEDA")
    print(" Come il modellino della scheda Impostazioni di Betaflight Configurator: muovi il drone in mano")
    print(" e sullo schermo il disegno fa lo stesso. Se si muove al contrario, la scheda è montata ruotata")
    print(" e Betaflight non lo sa: in volo il drone sarebbe ingovernabile.")
    print(" Serve solo il cavo USB, niente batteria e niente radio.")
    rap = []
    momento = datetime.now()
    if not A.conferma("Drone collegato con la USB e appoggiato in piano sul tavolo?"):
        return None
    correzione = None
    with A.apri() as ser:
        allin = allineamento(ser)
        acc = ha_accelerometro(ser)
        if not acc:
            rap.append(("NOTA", "Accelerometro spento",
                        A.capito("Senza accelerometro le modalità Angle/Horizon e il GPS Rescue non funzionano e il "
                                 "disegno può andare alla deriva. Per chi vola solo in Acro va bene.",
                                 "Se ti servono Angle o GPS Rescue, attivalo in Configurator (scheda Configurazione)."),
                        None))
        A.istruisci("Lascia il drone fermo e in piano sul tavolo, telecamera in avanti", False)
        piano = segui(ser, lambda a: True, tenuta=2, limite=6)
        if piano and abs(piano[0]) > 150:
            rap.append(("ATTENZIONE", "La scheda crede di essere capovolta",
                        A.capito("Con il drone dritto la scheda legge circa 180° di inclinazione: è montata a testa in "
                                 "giù (o il giroscopio è ruotato) e Betaflight non lo sa.",
                                 "In Configurator, scheda Configurazione, Allineamento della scheda: rollio 180. "
                                 "Poi ripeti questa prova."), None))
            A.stampa_rapporto(rap)
            A.salva_rapporto(drone, "assetto", rap)
            return rap
        inclinato = piano and acc and (abs(piano[0]) > 3 or abs(piano[1]) > 3)

        A.istruisci("Prendi il drone e INCLINALO VERSO DESTRA (lato destro in basso, circa 45°). Tienilo così",
                    True)
        a = segui(ser, lambda a: max(abs(a[0]), abs(a[1])) > 25, tenuta=1.0)
        if a is None:
            rap.append(("VERIFICA", "Non ho visto l'inclinazione a destra",
                        A.capito("Il disegno non si è mosso abbastanza nel tempo concesso.",
                                 "Ripeti la prova inclinando di più il drone."), None))
        elif a[0] > 25:
            rap.append(("OK", "Inclinazione a destra: il drone sullo schermo fa lo stesso", A.capito(
                "Il rollio è letto nel verso giusto."), None))
        elif a[0] < -25:
            correzione = (allin[2] + 180) % 360
            rap.append(("ATTENZIONE", "Inclinazione al contrario: la scheda è girata di 180°",
                        A.capito("Hai inclinato a destra e la scheda ha sentito sinistra: la freccia della scheda "
                                 "punta all'indietro rispetto alla telecamera.",
                                 f"Si corregge con l'allineamento della scheda (board_align_yaw = {correzione}). "
                                 "Posso farlo io."), None))
        else:
            correzione = (allin[2] + (90 if a[1] > 0 else 270)) % 360
            rap.append(("ATTENZIONE", "Inclinazione letta sull'asse sbagliato: la scheda è girata di 90°",
                        A.capito("Hai inclinato a destra e la scheda ha sentito il muso alzarsi o abbassarsi: la "
                                 "freccia della scheda punta di lato.",
                                 f"Si corregge con l'allineamento della scheda (board_align_yaw = {correzione}). "
                                 "Posso farlo io, e dopo verifichiamo di nuovo."), None))

        if correzione is None and a is not None:
            A.istruisci("Ora ALZA IL MUSO (telecamera verso l'alto, circa 45°). Tienilo così", True)
            b = segui(ser, lambda x: abs(x[1]) > 25, tenuta=1.0)
            if b is None:
                rap.append(("VERIFICA", "Non ho visto il muso alzarsi",
                            A.capito("Il disegno non si è mosso abbastanza.", "Ripeti alzando di più il muso."), None))
            else:
                rap.append(("OK", "Muso in alto: il drone sullo schermo fa lo stesso",
                            A.capito("Anche il beccheggio è letto correttamente."), None))

            A.istruisci("Rimetti il drone in piano e GIRALO PIANO IN SENSO ORARIO (visto dall'alto) di un quarto "
                        "di giro", True)
            inizio = assetto(ser)
            c = segui(ser, lambda x: inizio and abs(differenza_angolo(x[2], inizio[2])) > 45, tenuta=0.5)
            if c is None or not inizio:
                rap.append(("VERIFICA", "Non ho visto il drone girare",
                            A.capito("La direzione (yaw) non è cambiata abbastanza.", "Ripeti girandolo di più."),
                            None))
            elif differenza_angolo(c[2], inizio[2]) > 0:
                rap.append(("OK", "Rotazione oraria letta nel verso giusto",
                            A.capito("Anche l'imbardata è corretta: la scheda conosce il suo orientamento."), None))
            else:
                rap.append(("ATTENZIONE", "Rotazione letta al contrario",
                            A.capito("Hai girato in senso orario e la scheda ha sentito antiorario: succede quando "
                                     "la scheda è montata capovolta.",
                                     "Controlla in Configurator l'allineamento della scheda (rollio 180) e ripeti."),
                            None))

        if inclinato and correzione is None:
            print(f"\nCon il drone in piano la scheda legge {gradi(piano[0])} di rollio e {gradi(piano[1])} di beccheggio.")
            A.istruisci("Rimetti il drone FERMO E IN PIANO sul tavolo (meglio se controlli con una livella)", True)
            if A.conferma("Calibro l'accelerometro adesso? (il drone deve restare fermo e in piano)"):
                A.msp(ser, MSP_ACC_CALIBRATION)
                print("Calibrazione in corso, non toccare il drone ...")
                time.sleep(3)
                dopo = segui(ser, lambda x: True, tenuta=1.5, limite=4)
                if dopo and abs(dopo[0]) <= 2 and abs(dopo[1]) <= 2:
                    rap.append(("OK", "Accelerometro calibrato",
                                A.capito(f"Ora in piano la scheda legge {gradi(dopo[0])} e {gradi(dopo[1])}: in Angle il "
                                         "drone non scivola più da un lato. La calibrazione si salva da sola."), None))
                else:
                    rap.append(("VERIFICA", "Dopo la calibrazione non è ancora in piano",
                                A.capito("Il drone si è mosso o il tavolo non è in piano.",
                                         "Ripeti su una superficie in piano, senza toccarlo."), None))
            else:
                rap.append(("NOTA", f"Accelerometro non calibrato ({gradi(piano[0])} / {gradi(piano[1])})",
                            A.capito("In Angle e Horizon il drone tenderebbe a scivolare da un lato.",
                                     "Ripeti la prova e accetta la calibrazione, con il drone in piano."), None))
        elif piano and acc and correzione is None:
            rap.append(("OK", "In piano la scheda legge in piano",
                        A.capito(f"Rollio {gradi(piano[0])}, beccheggio {gradi(piano[1])}: l'accelerometro è tarato."),
                        None))
    A.stampa_rapporto(rap)
    A.salva_rapporto(drone, "assetto", rap)
    salva_testo(drone, "assetto", momento, rap)
    if correzione is not None:
        print("\nCORREZIONE: dico a Betaflight come è montata la scheda.")
        A.applica_modifica(drone, "board_align_yaw",
                           {"consigliato": str(correzione), "attuale": str(allin[2]), "al_banco": True,
                            "prova": ["Ripeti 'Assetto della scheda': il disegno deve seguire il drone",
                                      "Solo dopo, primo volo in un posto aperto"]})
    return rap


# ============================================================== 3. vibrazioni dei motori

def campiona(g, secondi):
    """Legge giroscopio e giri del motore per 'secondi'. Restituisce (rumore, giri medi, irregolarità)."""
    gyro, giri = [], []
    inizio = time.time()
    while time.time() - inizio < secondi:
        r = g.msp(MSP_RAW_IMU)
        if r and len(r) >= 12:
            gyro.append(struct.unpack("<hhh", r[6:12]))
        if len(gyro) % 5 == 0:
            try:
                with g.lucchetto:
                    t = A.leggi_telemetria(g.ser)
                if t and g.motore:
                    giri.append(t[g.motore - 1][0])
            except Exception:   # noqa: BLE001
                pass
    if len(gyro) < 5:
        return None, 0, 0
    rumore = 0.0
    for asse in range(3):
        v = [x[asse] for x in gyro]
        m = sum(v) / len(v)
        rumore += sum((x - m) ** 2 for x in v) / len(v)
    rumore = (rumore / 3) ** 0.5
    giri = [x for x in giri if x > 0]
    media = sum(giri) / len(giri) if giri else 0
    irreg = ((sum((x - media) ** 2 for x in giri) / len(giri)) ** 0.5 / media) if media else 0
    return rumore, media, irreg


def menu_vibrazioni(drone):
    print("\nVIBRAZIONI DEI MOTORI")
    print(" Faccio girare ogni motore da solo per qualche secondo, senza eliche, e ascolto il giroscopio.")
    print(" Un motore con il cuscinetto rovinato, l'albero storto o una vite allentata fa vibrare la scheda")
    print(" molto più degli altri. È un confronto tra i quattro motori: la parola finale resta l'analisi del volo.")
    rap = []
    momento = datetime.now()
    if not batteria_ok(drone, rap):
        A.stampa_rapporto(rap)
        A.salva_rapporto(drone, "vibrazioni", rap)
        return rap
    print("\n 1. SMONTA TUTTE LE ELICHE.")
    print(" 2. Radio spenta o drone disarmato, cavo USB collegato, batteria attaccata.")
    print(" 3. Drone appoggiato sul tavolo su una superficie rigida, e non toccarlo durante la prova.")
    print(" I motori gireranno a circa un quarto del massimo: fanno rumore, è normale.")
    if not A.conferma("Eliche SMONTATE, batteria attaccata, drone fermo sul tavolo?"):
        print("Annullato: nessun motore ha girato.")
        return None
    misure = {}
    with A.apri() as ser:
        try:
            if not tensione_ok(ser, drone, rap):
                A.stampa_rapporto(rap)
                A.salva_rapporto(drone, "vibrazioni", rap)
                return rap
            with Gira(ser, 0, 1000) as g:
                print("Misuro la scheda a motori fermi ...")
                fermo, _, _ = campiona(g, 2)
            for n in range(1, 5):
                print(f"Motore {n} ({POSIZIONI[n]}) ...")
                with Gira(ser, n, GIRO_PROVA) as g:
                    mostra_motore(n)
                    time.sleep(1.5)            # il motore prende i giri
                    misure[n] = campiona(g, 4)
                print()
                time.sleep(1)
        finally:
            ferma_tutto(ser)
            print("\nMotori fermi. Puoi staccare la batteria.\n")

    valide = {n: m for n, m in misure.items() if m[0] is not None}
    if len(valide) < 4 or fermo is None:
        rap.append(("VERIFICA", "Non sono riuscito a leggere il giroscopio",
                    A.capito("La scheda non ha mandato abbastanza dati durante la prova.", "Ripeti la prova."), None))
    else:
        print("Vibrazione letta dal giroscopio (più alto = vibra di più):")
        print(f"  a motori fermi: {fermo:.1f}".replace(".", ","))
        for n, (r, giri, irr) in valide.items():
            extra = f"   {A.migliaia(round(giri))} giri/min" if giri else ""
            print(f"  motore {n} ({POSIZIONI[n]}): " + f"{r:.1f}".replace(".", ",") + extra)
        ordinati = sorted(r for r, _, _ in valide.values())
        riferimento = (ordinati[1] + ordinati[2]) / 2 or 1
        brutti, dubbi = [], []
        for n, (r, _, _) in valide.items():
            rapporto = r / riferimento
            if rapporto >= 1.8:
                brutti.append((n, rapporto))
            elif rapporto >= 1.4:
                dubbi.append((n, rapporto))
        for n, rapporto in brutti:
            rap.append(("ATTENZIONE", f"Il motore {n} ({POSIZIONI[n]}) vibra molto più degli altri "
                                      "(" + f"{rapporto:.1f}".replace(".", ",") + " volte)",
                        A.capito("A parità di giri fa tremare la scheda molto più degli altri tre: cuscinetto "
                                 "rovinato, albero o campana storti dopo un urto, magnete scollato o vite allentata.",
                                 "Gira la campana a mano: se gratta o ha gioco, cambia il cuscinetto o il motore. "
                                 "Controlla le viti del motore e che l'albero sia dritto."), None))
        for n, rapporto in dubbi:
            rap.append(("VERIFICA", f"Il motore {n} ({POSIZIONI[n]}) vibra un po' più degli altri "
                                    "(" + f"{rapporto:.1f}".replace(".", ",") + " volte)",
                        A.capito("Non è grave, ma è il primo da tenere d'occhio.",
                                 "Gira la campana a mano e senti se scorre liscia; controlla le viti."), None))
        irregolari = [n for n, (_, giri, irr) in valide.items() if giri and irr > 0.08]
        for n in irregolari:
            rap.append(("VERIFICA", f"Il motore {n} ({POSIZIONI[n]}) non tiene i giri costanti",
                        A.capito("A comando fisso i giri cambiano parecchio: può essere un problema di ESC, di "
                                 "saldatura o di avvolgimento.",
                                 "Ripeti la prova; se si ripete, controlla saldature e ESC di quel motore."), None))
        if not brutti and not dubbi and not irregolari:
            rap.append(("OK", "I quattro motori vibrano in modo simile",
                        A.capito("Nessun motore fa tremare la scheda più degli altri: cuscinetti e alberi sembrano "
                                 "a posto. I problemi delle eliche si vedono solo in volo (Analizza un volo)."), None))
        if not any(g for _, g, _ in valide.values()):
            rap.append(("NOTA", "Giri dei motori non disponibili",
                        A.capito("Senza DShot bidirezionale non posso controllare se i giri restano costanti.",
                                 "Non serve per questa prova; il DShot bidirezionale si attiva da Correggi e "
                                 "migliora."), None))
    A.stampa_rapporto(rap)
    A.salva_rapporto(drone, "vibrazioni", rap)
    salva_testo(drone, "vibrazioni", momento, rap)
    return rap


# ============================================================== registro

def salva_testo(drone, tipo, momento, rap):
    percorso = os.path.join(A.cartella(drone["nome"]), f"{tipo}_{momento:%Y-%m-%d_%H-%M}.txt")
    with open(percorso, "w", encoding="utf-8") as f:
        f.write(f"{A.FONTI[tipo]} - {drone['nome']} - {momento:%d/%m/%Y %H:%M}\n\n")
        for livello, titolo, perche, _ in rap:
            f.write(f"[{livello}] {titolo}\n{A.rientra(perche)}\n")
    print(f"\nRapporto salvato in: {percorso}")
