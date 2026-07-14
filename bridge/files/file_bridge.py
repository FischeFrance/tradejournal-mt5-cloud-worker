"""mt5-bridge reale, basato su file JSON scritti da un Expert Advisor MQL5 read-only
(mt5/experts/TradeJournalBridge.mq5) invece che sul pacchetto Python Windows `MetaTrader5`.

Nel runtime containerizzato, `mt5.initialize()` del pacchetto Python Windows sotto Wine fallisce
stabilmente con `(-10005, "IPC timeout")`, mentre il terminale MetaTrader 5 stesso si avvia
regolarmente sotto Wine. Questo modulo elimina il secondo processo Windows: legge invece i file
che l'EA scrive nel proprio sandbox (`MQL5/Files/TradeJournal`, montato per questo processo Linux
tramite `MT5_EA_FILES_DIR`, un semplice path Unix calcolato da deploy/instance/entrypoint-runtime.sh
-- questo modulo non sa nulla di Wine/WINEPREFIX, esattamente come richiesto dal docstring di
bridge/common.py).

Implementa lo stesso contratto HTTP di bridge/fake/fake_bridge.py e del precedente
bridge/windows/mt5_bridge.py (vedi bridge/common.py): GET /health, POST /v1/candles e
POST /v1/trading/snapshot. Nessun endpoint di trading: questo processo non importa MetaTrader5,
non ha mai credenziali MT5 (login/password/server) e non potrebbe comunque inviare un ordine,
avendo solo accesso in lettura ai file prodotti dall'EA.

Login/server del payload di /v1/trading/snapshot NON sono mascherati (arrivano cosi' da
account.json, scritto dall'EA): viaggiano solo sulla rete Docker interna verso il worker, che li
richiede non vuoti per attribuire le operazioni. Sono invece mascherati nella risposta di
/health e in ogni messaggio di log, coerente con la convenzione gia' usata altrove nel repo.

Verifica obbligatoria dell'identita' dell'account: TJ_EXPECTED_MT5_LOGIN/TJ_EXPECTED_MT5_SERVER
(iniettati dal provisioning, stessi valori di MT5_LOGIN/MT5_SERVER passati all'entrypoint per
generare startup.ini) devono coincidere esattamente con quanto l'EA scrive in account.json. Se
non coincidono, l'istanza e' considerata unhealthy e NESSUNO snapshot o evento viene inoltrato
(vedi FileSnapshotSource._identity_matches) -- una configurazione errata (secret montato sul
container sbagliato, golden template riusato per un altro account, ecc.) non deve mai risultare
in dati attribuiti silenziosamente a una connessione diversa da quella prevista.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import (  # noqa: E402
    MAX_DEAL_LOOKBACK_HOURS,
    TIMEFRAME_SECONDS,
    BaseBridgeHandler,
    BridgeConfig,
    BridgeError,
    format_iso_utc,
    parse_iso_utc,
    read_secret_from_env,
)

#: Nome dei file scritti da mt5/experts/TradeJournalBridge.mq5 sotto MT5_EA_FILES_DIR. Vedi il
#: docstring in testa a quel file per lo schema esatto di ciascuno.
HEARTBEAT_FILE = "heartbeat.json"
ACCOUNT_FILE = "account.json"
POSITIONS_FILE = "positions.json"
ORDERS_FILE = "orders.json"
CANDLES_FILE = "candles.json"
EVENTS_FILE = "events.jsonl"

#: Generazione precedente di events.jsonl dopo una rotazione (vedi mt5/experts/
#: TradeJournalBridge.mq5:RotateEventsLogIfNeeded). Una sola generazione storica e' conservata.
ROTATED_EVENTS_FILE = "events.jsonl.1"

#: File di cursore di proprieta' di QUESTO processo (mai scritto dall'EA): evita di
#: ri-scansionare l'intero events.jsonl da zero a ogni riavvio del solo processo bridge.
BRIDGE_CURSOR_FILE = "bridge_cursor.json"

#: Identita' di una connessione/account: (connection_id, login, server). Usata sia come parte
#: della chiave di deduplica dei deal (mai il solo deal_ticket, vedi _EventsCursor) sia per
#: filtrare eventi che non appartengono all'identita' attesa da questa istanza.
Identity = Tuple[str, str, str]

DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 15.0

#: Tolleranza per un heartbeat "nel futuro" di pochi secondi (troncamento a secondo intero nella
#: formattazione lato EA): non deve mai far apparire un heartbeat appena scritto come scaduto.
_HEARTBEAT_CLOCK_SKEW_TOLERANCE_SECONDS = 2.0


def _mask(value: Optional[str]) -> str:
    if not value:
        return "<vuoto>"
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def _positive_float(value: Optional[str], default: float, name: str) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} deve essere numerico.") from exc
    if parsed <= 0:
        raise ValueError(f"{name} deve essere positivo.")
    return parsed


def _read_json(path: Path) -> Any:
    """Non solleva mai: file assente/parziale/corrotto sono uno stato normale e transitorio
    durante l'avvio dell'EA o mentre una scrittura atomica e' in corso altrove."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    value = _read_json(path)
    return value if isinstance(value, dict) else None


def _read_json_array(path: Path) -> Optional[List[Any]]:
    value = _read_json(path)
    return value if isinstance(value, list) else None


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError:
        # La persistenza del cursore e' un'ottimizzazione (evita una ri-scansione completa al
        # prossimo avvio): un errore qui non deve mai impedire al bridge di rispondere.
        pass


def _not_ready(detail: str) -> BridgeError:
    return BridgeError(503, "mt5_not_connected", f"file-bridge non pronto: {detail}")


def _parse_account_file(raw: object) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise _not_ready("account.json non e' un oggetto JSON.")
    try:
        return {
            "login": str(raw["login"]),
            "server": str(raw["server"]),
            "balance": float(raw["balance"]),
            "equity": float(raw["equity"]),
            "currency": str(raw["currency"]),
            "leverage": int(raw["leverage"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise _not_ready("account.json ha uno schema inatteso.") from exc


def _parse_position_file(raw: object) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise _not_ready("positions.json contiene una voce non valida.")
    try:
        direction = str(raw["direction"])
        if direction not in ("buy", "sell"):
            raise ValueError("direction non valida")
        return {
            "ticket": str(raw["ticket"]),
            "symbol": str(raw["symbol"]),
            "direction": direction,
            "volume": float(raw["volume"]),
            "open_price": float(raw["open_price"]),
            "stop_loss": float(raw["stop_loss"]),
            "take_profit": float(raw["take_profit"]),
            "open_time": str(raw["open_time"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise _not_ready("positions.json ha uno schema inatteso.") from exc


def _parse_order_file(raw: object) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise _not_ready("orders.json contiene una voce non valida.")
    try:
        direction = str(raw["direction"])
        if direction not in ("buy", "sell"):
            raise ValueError("direction non valida")
        return {
            "ticket": str(raw["ticket"]),
            "symbol": str(raw["symbol"]),
            "direction": direction,
            "volume": float(raw["volume"]),
            "price": float(raw["price"]),
            "stop_loss": float(raw["stop_loss"]),
            "take_profit": float(raw["take_profit"]),
            "order_type": int(raw["order_type"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise _not_ready("orders.json ha uno schema inatteso.") from exc


def _parse_candle_file(raw: object) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise BridgeError(502, "mt5_error", "candles.json contiene una voce non valida.")
    try:
        return {
            "open_time": str(raw["open_time"]),
            "open": str(raw["open"]),
            "high": str(raw["high"]),
            "low": str(raw["low"]),
            "close": str(raw["close"]),
            "tick_volume": int(raw["tick_volume"]),
            "spread": int(raw["spread"]),
            "source": str(raw.get("source", "mt5")),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeError(502, "mt5_error", "candles.json ha uno schema inatteso.") from exc


def _file_identity(path: Path) -> Optional[Tuple[int, int]]:
    """Fingerprint POSIX (device, inode) di un file: cambia quando il file viene ruotato o
    ricreato con lo stesso nome, resta invariato quando viene solo troncato o esteso sul posto."""
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_dev, stat_result.st_ino)


#: Finestra usata da _tail_fingerprint: fissa e piccola, cosi' il controllo resta economico
#: indipendentemente da quanto e' cresciuto events.jsonl (che comunque non supera InpEventsMaxBytes
#: grazie alla rotazione lato EA, vedi mt5/experts/TradeJournalBridge.mq5).
_TAIL_FINGERPRINT_WINDOW_BYTES = 256


def _tail_fingerprint(path: Path, offset: int) -> Optional[str]:
    """Hash degli ultimi 'min(offset, _TAIL_FINGERPRINT_WINDOW_BYTES)' byte del file fino alla
    posizione 'offset'. Il solo confronto (device, inode) + dimensione non basta a rilevare un
    troncamento-e-riscrittura sul posto quando il nuovo contenuto non e' piu' corto del
    precedente (stesso file, stessa o maggiore dimensione, ma byte diversi prima dell'offset
    persistito): questa firma lo rileva comunque."""
    if offset <= 0:
        return ""
    window_start = max(0, offset - _TAIL_FINGERPRINT_WINDOW_BYTES)
    try:
        with open(path, "rb") as handle:
            handle.seek(window_start)
            window = handle.read(offset - window_start)
    except OSError:
        return None
    return hashlib.sha256(window).hexdigest()


def _event_to_deal(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Converte una riga DEAL_ADD/entry-di-uscita di events.jsonl in un record interno con
    identita' completa (connection_id, login, server) piu' i campi del contratto 'deal'
    (vedi _public_deal). Restituisce None per una riga malformata o priva dei campi di identita'
    richiesti: viene ignorata invece di far fallire l'intera lettura incrementale, e non finisce
    mai nell'indice senza un'identita' nota (nessuna chiave 'orfana')."""
    position_id = event.get("position_id")
    if position_id is None:
        return None
    try:
        connection_id = str(event["connection_id"])
        login = str(event["login"])
        server = str(event["server"])
        deal_ticket = str(event["ticket"])
        position_ticket = str(position_id)
        close_price = float(event["price"])
        profit = float(event["profit"])
        commission = float(event["commission"])
        swap = float(event["swap"])
        close_time = str(event["time"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all((connection_id, login, server, deal_ticket, position_ticket, close_time)):
        return None

    # Chiave composita: due connessioni/account diversi (broker/demo differenti che riusano la
    # stessa numerazione ticket) non vengono MAI fusi in una voce sola -- a differenza di una
    # deduplica per solo deal_ticket. \x1f (unit separator) non compare mai in un login/ticket
    # numerico ne', in pratica, in un nome server o in uno UUID di connessione.
    key = "\x1f".join((connection_id, login, server, deal_ticket))
    return {
        "key": key,
        "identity": (connection_id, login, server),
        "deal_ticket": deal_ticket,
        "position_ticket": position_ticket,
        "close_price": close_price,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "close_time": close_time,
    }


def _public_deal(record: Dict[str, Any]) -> Dict[str, Any]:
    """Proietta un record interno (con identity/key aggiuntivi) sullo schema pubblico 'deal' del
    contratto /v1/trading/snapshot, invariato rispetto al vecchio bridge/windows/mt5_bridge.py."""
    return {
        "deal_ticket": record["deal_ticket"],
        "position_ticket": record["position_ticket"],
        "close_price": record["close_price"],
        "profit": record["profit"],
        "commission": record["commission"],
        "swap": record["swap"],
        "close_time": record["close_time"],
    }


class _EventsCursor:
    """Lettura incrementale di events.jsonl con cursore persistito composto da: identita' del
    file (device+inode), offset in byte dentro quel file, e indice dei deal di chiusura gia'
    visti per (connection_id, login, server, deal_ticket) -- mai il solo deal_ticket, cosi' due
    connessioni/account diversi con ticket numericamente coincidenti non vengono mai fusi in una
    voce sola. Un evento ripetuto (backfill dell'EA rieseguito, o lo stesso deal ri-notificato
    dopo un riavvio del terminale) sovrascrive semplicemente la voce esistente con la stessa
    chiave: la deduplica e' quindi corretta indipendentemente da eventuali collisioni di
    event_id, e idempotente rispetto a un riavvio del bridge fra la lettura e il salvataggio del
    cursore (le stesse righe vengono semplicemente rilette e producono lo stesso risultato).

    Rotazione: l'identita' (device, inode) permette di distinguere "il file e' cresciuto" (stesso
    file, si continua dall'offset persistito) da "il file e' stato ruotato o ricreato" (identita'
    diversa: vedi _sync). In quel caso, se events.jsonl.1 e' esattamente il file che si stava
    leggendo (stessa identita' del cursore), la sua coda non ancora letta viene consumata prima
    di ripartire da zero sul nuovo events.jsonl: nessuna riga completa scritta prima della
    rotazione va persa. Limite noto: se il bridge resta fermo abbastanza a lungo da perdere PIU'
    di una rotazione, la generazione intermedia (sovrascritta da una rotazione successiva) non e'
    piu' recuperabile; viene loggato un avviso esplicito in quel caso, mai un fallimento silente.

    Troncamento sul posto (stessa identita', contenuto prima dell'offset cambiato senza che il
    file sia stato ruotato): rilevato da _tail_fingerprint, non dal solo confronto di dimensione
    (che da solo non basterebbe se il nuovo contenuto non e' piu' corto del precedente).
    """

    def __init__(self, base_dir: Path) -> None:
        self._events_path = base_dir / EVENTS_FILE
        self._rotated_path = base_dir / ROTATED_EVENTS_FILE
        self._cursor_path = base_dir / BRIDGE_CURSOR_FILE
        self._offset = 0
        self._identity: Optional[Tuple[int, int]] = None
        self._tail_fingerprint: str = ""
        self._deals: Dict[str, Dict[str, Any]] = {}
        self._load_cursor()

    def _load_cursor(self) -> None:
        state = _read_json_object(self._cursor_path)
        if not isinstance(state, dict):
            return
        offset = state.get("offset")
        device = state.get("device")
        inode = state.get("inode")
        tail_fingerprint = state.get("tail_fingerprint")
        deals = state.get("deals")
        if isinstance(offset, int) and offset >= 0:
            self._offset = offset
        if isinstance(device, int) and isinstance(inode, int):
            self._identity = (device, inode)
        if isinstance(tail_fingerprint, str):
            self._tail_fingerprint = tail_fingerprint
        if isinstance(deals, dict):
            restored: Dict[str, Dict[str, Any]] = {}
            for key, record in deals.items():
                if not isinstance(record, dict):
                    continue
                identity = record.get("identity")
                if isinstance(identity, list) and len(identity) == 3:
                    record = dict(record)
                    record["identity"] = tuple(identity)  # JSON non ha tuple: round-trip esplicito
                restored[str(key)] = record
            self._deals = restored

    def _save_cursor(self) -> None:
        payload: Dict[str, Any] = {
            "offset": self._offset,
            "tail_fingerprint": self._tail_fingerprint,
            "deals": self._deals,
        }
        if self._identity is not None:
            payload["device"], payload["inode"] = self._identity
        _write_json_atomic(self._cursor_path, payload)

    def _prune_old_deals(self) -> None:
        # Limite bounded: nessun consumatore puo' mai chiedere piu' di MAX_DEAL_LOOKBACK_HOURS
        # (vedi bridge/common.py), quindi conservare oltre quella finestra non serve a nulla ed
        # eviterebbe che cursore/memoria crescano senza limite nel tempo.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_DEAL_LOOKBACK_HOURS)
        surviving = {}
        for key, record in self._deals.items():
            try:
                close_time = parse_iso_utc(record["close_time"], "close_time")
            except (BridgeError, KeyError):
                continue
            if close_time >= cutoff:
                surviving[key] = record
        self._deals = surviving

    def _consume_chunk(self, chunk: bytes) -> bool:
        """Elabora le righe complete di 'chunk' (separate da newline) in self._deals. Restituisce
        True se almeno un deal e' stato aggiunto o aggiornato. Non tocca offset/identita': quelli
        sono responsabilita' del chiamante, che conosce il file di provenienza del chunk."""
        changed = False
        for raw_line in chunk.split(b"\n"):
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # riga corrotta: ignorata, non deve mai interrompere la lettura
            if not isinstance(event, dict):
                continue
            if event.get("event_type") != "DEAL_ADD" or event.get("entry") not in ("OUT", "OUT_BY"):
                continue
            record = _event_to_deal(event)
            if record is not None:
                self._deals[record["key"]] = record
                changed = True
        return changed

    @staticmethod
    def _read_complete_tail(path: Path, offset: int) -> bytes:
        """Legge da 'offset' alla fine di 'path' e restituisce solo il prefisso fino all'ultimo
        newline completo: l'ultima riga puo' essere ancora in scrittura e va riprovata al
        prossimo giro. b"" se il file non e' apribile o non contiene righe complete oltre offset."""
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            return b""
        last_newline = chunk.rfind(b"\n")
        if last_newline < 0:
            return b""
        return chunk[: last_newline + 1]

    def _sync(self) -> None:
        current_identity = _file_identity(self._events_path)
        if current_identity is None:
            return  # events.jsonl non ancora creato dall'EA: nessun deal disponibile per ora

        if self._identity is not None and current_identity != self._identity:
            # Identita' diversa da quella tracciata: rotazione o rigenerazione. Se il file
            # precedente e' rintracciabile in events.jsonl.1 con la STESSA identita' che stavamo
            # seguendo, e' la rotazione fatta da RotateEventsLogIfNeeded: consuma prima la sua
            # coda non ancora letta.
            if _file_identity(self._rotated_path) == self._identity:
                tail = self._read_complete_tail(self._rotated_path, self._offset)
                if tail and self._consume_chunk(tail):
                    self._prune_old_deals()
            else:
                sys.stderr.write(
                    "[file-bridge] WARNING: events.jsonl risulta ruotato/ricreato piu' volte "
                    "dall'ultima lettura di questo bridge: eventuali deal scritti prima di "
                    "questo punto e non ancora letti potrebbero mancare dallo storico esposto.\n"
                )
            self._offset = 0
            self._tail_fingerprint = ""
            self._identity = current_identity
        elif self._identity is None:
            self._identity = current_identity

        try:
            current_size = self._events_path.stat().st_size
        except OSError:
            current_size = 0

        if current_size < self._offset:
            # Piu' corto del cursore (stessa identita'): non e' una rotazione, ma il vecchio
            # offset non e' piu' valido per questo file.
            self._offset = 0
            self._tail_fingerprint = ""
        else:
            current_fingerprint = _tail_fingerprint(self._events_path, self._offset)
            if current_fingerprint is not None and current_fingerprint != self._tail_fingerprint:
                # Stessa identita' e dimensione non piu' corta, ma il contenuto prima
                # dell'offset persistito e' cambiato: troncamento-e-riscrittura sul posto che il
                # solo confronto di dimensione non avrebbe rilevato.
                self._offset = 0
                self._tail_fingerprint = ""

        tail = self._read_complete_tail(self._events_path, self._offset)
        if tail:
            self._offset += len(tail)
            if self._consume_chunk(tail):
                self._prune_old_deals()

        new_fingerprint = _tail_fingerprint(self._events_path, self._offset)
        if new_fingerprint is not None:
            self._tail_fingerprint = new_fingerprint  # "" e' un fingerprint valido (offset 0)
        self._save_cursor()

    def deals_within(self, lookback_hours: int, expected_identity: Identity) -> List[Dict[str, Any]]:
        self._sync()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        result = []
        for record in self._deals.values():
            if record.get("identity") != expected_identity:
                continue  # evento di una connessione/account diversi: mai esposto da questa istanza
            try:
                close_time = parse_iso_utc(record["close_time"], "close_time")
            except BridgeError:
                continue
            if close_time >= cutoff:
                result.append(_public_deal(record))
        result.sort(key=lambda d: d["close_time"])
        return result


class FileSnapshotSource:
    """Sorgente dati per il file-bridge: legge gli snapshot scritti da
    mt5/experts/TradeJournalBridge.mq5 sotto ``base_dir`` (MT5_EA_FILES_DIR).

    ``expected_login``/``expected_server`` sono il controllo obbligatorio di identita'
    dell'account (TJ_EXPECTED_MT5_LOGIN/TJ_EXPECTED_MT5_SERVER): se account.json non coincide,
    l'istanza e' unhealthy e ne' /health ne' /v1/trading/snapshot restituiscono mai dati reali
    (vedi _identity_matches)."""

    def __init__(
        self,
        base_dir: Path,
        heartbeat_max_age_seconds: float,
        connection_id: str,
        expected_login: str,
        expected_server: str,
    ) -> None:
        self._dir = base_dir
        self._max_age = heartbeat_max_age_seconds
        self._connection_id = connection_id
        self._expected_login = expected_login
        self._expected_server = expected_server
        self._events = _EventsCursor(base_dir)

    def _identity_matches(self, login: Optional[str], server: Optional[str]) -> bool:
        return login == self._expected_login and server == self._expected_server

    def _log_account_mismatch(self, actual_login: Optional[str], actual_server: Optional[str]) -> None:
        sys.stderr.write(
            "[file-bridge] WARNING: account.json non coincide con l'identita' attesa "
            f"(atteso login={_mask(self._expected_login)} server={_mask(self._expected_server)}, "
            f"effettivo login={_mask(actual_login)} server={_mask(actual_server)}); "
            "nessuno snapshot o evento verra' inoltrato finche' non e' risolto.\n"
        )

    def _heartbeat_status(self) -> tuple[bool, bool]:
        """Restituisce (heartbeat_fresco, terminale_connesso_secondo_l_EA)."""
        heartbeat = _read_json_object(self._dir / HEARTBEAT_FILE)
        if heartbeat is None:
            return False, False
        generated_at = heartbeat.get("generated_at")
        if not isinstance(generated_at, str):
            return False, False
        try:
            parsed = parse_iso_utc(generated_at, "generated_at")
        except BridgeError:
            return False, False
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
        fresh = -_HEARTBEAT_CLOCK_SKEW_TOLERANCE_SECONDS <= age <= self._max_age
        terminal_connected = heartbeat.get("terminal_connected") is True
        return fresh, terminal_connected

    def health(self) -> Dict[str, Any]:
        heartbeat_fresh, ea_terminal_connected = self._heartbeat_status()
        account = _read_json_object(self._dir / ACCOUNT_FILE) if heartbeat_fresh else None
        account_login = account.get("login") if isinstance(account, dict) else None
        account_server = account.get("server") if isinstance(account, dict) else None
        identity_present = (
            isinstance(account_login, str)
            and bool(account_login)
            and isinstance(account_server, str)
            and bool(account_server)
        )
        identity_ok = identity_present and self._identity_matches(account_login, account_server)
        if identity_present and not identity_ok:
            self._log_account_mismatch(account_login, account_server)

        account_connected = heartbeat_fresh and identity_ok
        terminal_connected = heartbeat_fresh and ea_terminal_connected
        status = "ok" if terminal_connected and account_connected else "degraded"
        return {
            "status": status,
            "terminal_connected": terminal_connected,
            "account_connected": account_connected,
            "server": _mask(account_server) if identity_present else "<vuoto>",
            "version": "file-bridge/1.0",
        }

    def get_trading_snapshot(self, deal_lookback_hours: int) -> Dict[str, Any]:
        heartbeat_fresh, _ = self._heartbeat_status()
        if not heartbeat_fresh:
            raise _not_ready("heartbeat.json assente o scaduto (EA/terminale non attivo).")

        account_raw = _read_json_object(self._dir / ACCOUNT_FILE)
        if account_raw is None:
            raise _not_ready("account.json non disponibile o non valido.")
        account = _parse_account_file(account_raw)

        if not self._identity_matches(account["login"], account["server"]):
            self._log_account_mismatch(account["login"], account["server"])
            raise BridgeError(
                503,
                "account_mismatch",
                "L'account connesso non coincide con l'identita' attesa per questa istanza: "
                "nessuno snapshot viene inoltrato.",
            )

        positions_raw = _read_json_array(self._dir / POSITIONS_FILE)
        if positions_raw is None:
            raise _not_ready("positions.json non disponibile o non valido.")
        orders_raw = _read_json_array(self._dir / ORDERS_FILE)
        if orders_raw is None:
            raise _not_ready("orders.json non disponibile o non valido.")

        positions = [_parse_position_file(item) for item in positions_raw]
        orders = [_parse_order_file(item) for item in orders_raw]
        expected_identity: Identity = (self._connection_id, self._expected_login, self._expected_server)
        deals = self._events.deals_within(deal_lookback_hours, expected_identity)

        return {
            "account": account,
            "positions": positions,
            "orders": orders,
            "deals": deals,
            "generated_at": format_iso_utc(datetime.now(timezone.utc)),
        }

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[datetime],
        now: datetime,
        limit: int,
    ) -> List[Dict[str, Any]]:
        candles_raw = _read_json_object(self._dir / CANDLES_FILE)
        if candles_raw is None:
            raise _not_ready("candles.json non disponibile o non valido.")
        by_symbol = candles_raw.get(symbol)
        if not isinstance(by_symbol, dict):
            raise BridgeError(
                422, "symbol_not_found", f"Il simbolo richiesto {symbol!r} non e' pubblicato dall'EA."
            )
        raw_list = by_symbol.get(timeframe)
        if not isinstance(raw_list, list):
            raise BridgeError(
                422, "unsupported_timeframe", f"Timeframe non pubblicato dall'EA: {timeframe!r}."
            )

        last_complete_epoch = int(now.timestamp()) - TIMEFRAME_SECONDS[timeframe]
        candles = []
        for raw_item in raw_list:
            candle = _parse_candle_file(raw_item)
            open_time = parse_iso_utc(candle["open_time"], "open_time")
            if int(open_time.timestamp()) > last_complete_epoch:
                continue  # candela ancora in formazione al momento 'now': mai restituita
            if since is not None and open_time <= since:
                continue  # since esclusivo, ricontrollato qui e non solo lato EA
            candles.append(candle)

        candles.sort(key=lambda c: c["open_time"])
        return candles[:limit]


class FileBridgeConfig(BridgeConfig):
    def __init__(self) -> None:
        super().__init__(
            token=read_secret_from_env("MT5_BRIDGE_TOKEN"),
            broker_symbol=os.environ.get("EURUSD_BROKER_SYMBOL") or "EURUSD",
            port=int(os.environ.get("PORT", "8080")),
            host=os.environ.get("HOST", "0.0.0.0"),
        )
        files_dir = os.environ.get("MT5_EA_FILES_DIR", "")
        if not files_dir:
            raise ValueError(
                "MT5_EA_FILES_DIR e' obbligatorio: deve puntare al sandbox "
                "MQL5/Files/TradeJournal dell'EA (impostato da entrypoint-runtime.sh)."
            )
        self.files_dir = Path(files_dir)
        self.heartbeat_max_age_seconds = _positive_float(
            os.environ.get("MT5_HEARTBEAT_MAX_AGE_SECONDS"),
            DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
            "MT5_HEARTBEAT_MAX_AGE_SECONDS",
        )

        self.connection_id = (os.environ.get("TJ_CONNECTION_ID") or "").strip()
        self.expected_login = (os.environ.get("TJ_EXPECTED_MT5_LOGIN") or "").strip()
        self.expected_server = (os.environ.get("TJ_EXPECTED_MT5_SERVER") or "").strip()
        if not self.connection_id:
            raise ValueError("TJ_CONNECTION_ID e' obbligatorio.")
        if not self.expected_login:
            raise ValueError(
                "TJ_EXPECTED_MT5_LOGIN e' obbligatorio: verifica dell'identita' dell'account."
            )
        if not self.expected_server:
            raise ValueError(
                "TJ_EXPECTED_MT5_SERVER e' obbligatorio: verifica dell'identita' dell'account."
            )


class Handler(BaseBridgeHandler):
    source: FileSnapshotSource  # impostata dinamicamente da make_server()

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_bridge_error(BridgeError(404, "not_found", f"Path non trovato: {self.path}"))
            return
        if not self.check_auth():
            return
        self.send_json(200, self.source.health())

    def do_POST(self) -> None:
        if self.path not in ("/v1/candles", "/v1/trading/snapshot"):
            self.send_bridge_error(BridgeError(404, "not_found", f"Path non trovato: {self.path}"))
            return
        if not self.check_auth():
            return
        try:
            request = self.read_json_body()
            if self.path == "/v1/trading/snapshot":
                deal_lookback_hours = self.parse_trading_snapshot_request(request)
                snapshot = self.source.get_trading_snapshot(deal_lookback_hours)
            else:
                symbol, timeframe, since, now, limit = self.parse_candles_request(request)
                candles = self.source.get_candles(symbol, timeframe, since, now, limit)
        except BridgeError as exc:
            self.send_bridge_error(exc)
            return
        if self.path == "/v1/trading/snapshot":
            self.send_json(200, snapshot)
        else:
            self.send_json(200, {"symbol": symbol, "timeframe": timeframe, "candles": candles})


def make_server(config: FileBridgeConfig, source: FileSnapshotSource) -> ThreadingHTTPServer:
    handler_cls = type("_FileBridgeHandler", (Handler,), {"config": config, "source": source})
    return ThreadingHTTPServer((config.host, config.port), handler_cls)


def main() -> None:
    config = FileBridgeConfig()
    source = FileSnapshotSource(
        config.files_dir,
        config.heartbeat_max_age_seconds,
        config.connection_id,
        config.expected_login,
        config.expected_server,
    )
    server = make_server(config, source)
    sys.stderr.write(
        f"[file-bridge] in ascolto su {config.host}:{config.port} "
        f"(files_dir={config.files_dir}, broker_symbol={config.broker_symbol})\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
