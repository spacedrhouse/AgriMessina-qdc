from datetime import datetime
from sqlalchemy import text

from app_logging import get_logger

log = get_logger(__name__)

def ricalcola_avvisi_globali(engine_or_conn):
    """
    Versione modificata per accettare sia l'engine che una connessione esistente.
    Questo evita l'apertura di transazioni annidate che bloccano il database.
    """
    if hasattr(engine_or_conn, "execute"):
        # Se engine_or_conn è già una connessione attiva, la usiamo direttamente
        _esegui_logica_avvisi(engine_or_conn)
    else:
        # Altrimenti apriamo una nuova transazione tramite l'engine
        with engine_or_conn.begin() as conn:
            _esegui_logica_avvisi(conn)

def _esegui_logica_avvisi(conn):
    """
    Contiene la logica effettiva di calcolo degli avvisi.
    Tutte le operazioni usano la connessione 'conn' passata come argomento.
    """
    try:
        # 1. Pulizia avvisi esistenti
        conn.execute(text("DELETE FROM avvisi_trattamenti"))
        avvisi_per_trattamento = {}

        # 0. Controllo Blacklist
        blacklist_viol = conn.execute(text("""
            SELECT t.id, ten.codice, p.nome_prodotto FROM trattamenti t
            JOIN dettaglio_trattamenti dt ON t.id = dt.trattamento_id
            JOIN tendoni ten ON ten.id = dt.tendone_id
            JOIN prodotti p ON p.id = t.prodotto_id
            WHERE LOWER(TRIM(p.blacklist)) = 'si'
        """)).fetchall()

        for t_id, codice, prodotto in blacklist_viol:
            testo_msg = f"⛔ ALLARME BLACKLIST su {codice}: Il prodotto «{prodotto}» è stato contrassegnato come VIETATO."
            avvisi_per_trattamento.setdefault(t_id, []).append(testo_msg)

        # 1. Controllo Bio/Conv
        bio_conv_viol = conn.execute(text("""
            SELECT t.id, ten.codice, p.nome_prodotto FROM trattamenti t
            JOIN dettaglio_trattamenti dt ON t.id = dt.trattamento_id
            JOIN tendoni ten ON ten.id = dt.tendone_id
            JOIN contrade c ON c.id = ten.contrada_id
            JOIN agri ag ON ag.id = c.agro_id
            JOIN aziende az ON az.id = ag.azienda_id
            JOIN prodotti p ON p.id = t.prodotto_id
            WHERE LOWER(TRIM(az.nome)) = 'agrimessina' AND LOWER(TRIM(p.bio_convenzionale)) = 'conv'
        """)).fetchall()

        for t_id, codice, prodotto in bio_conv_viol:
            testo_msg = f"⚠️ Tendone {codice}: Prodotto Convenzionale («{prodotto}») in azienda Biologica."
            avvisi_per_trattamento.setdefault(t_id, []).append(testo_msg)

        # 2. Controllo Dose
        dose_viol = conn.execute(text("""
            SELECT t.id, p.nome_prodotto, ten.codice,
                   SUM(dt.quantita_sostanza) AS qta_netta,
                   SUM(CASE WHEN dt.botti > 0 THEN dt.botti ELSE 0 END) AS botti_netti,
                   ten.ettari, p.min_sostanza, p.max_sostanza, p.unita_misura,
                   tt.tipo
            FROM trattamenti t
            JOIN dettaglio_trattamenti dt ON t.id = dt.trattamento_id
            JOIN tendoni ten ON ten.id = dt.tendone_id
            JOIN prodotti p ON p.id = t.prodotto_id
            JOIN (
                SELECT id AS trat_id,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM dettaglio_trattamenti dt2
                           WHERE dt2.trattamento_id = trattamenti.id
                             AND (dt2.is_bilanciamento = 0 OR dt2.is_bilanciamento IS NULL)
                       ) THEN 'normale' ELSE 'solo_bil' END AS tipo
                FROM trattamenti
            ) tt ON tt.trat_id = t.id
            WHERE ((p.min_sostanza IS NOT NULL AND p.min_sostanza > 0)
                   OR (p.max_sostanza IS NOT NULL AND p.max_sostanza > 0))
              AND (
                  (tt.tipo = 'normale' AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL))
                  OR
                  (tt.tipo = 'solo_bil')
              )
            GROUP BY t.id, dt.tendone_id, p.nome_prodotto, ten.codice, ten.ettari,
                     p.min_sostanza, p.max_sostanza, p.unita_misura, tt.tipo
        """)).fetchall()

        for row in dose_viol:
            t_id, prodotto, codice, qta_netta, botti_netti, ettari, min_s, max_s, um, _tipo = row
            if qta_netta is None or qta_netta <= 0:
                continue
            um_str = str(um).strip().lower() if um else "unità/ha"

            if '/hl' in um_str:
                volume_teorico = ettari * 10.0 if ettari and ettari > 0 else 0
                dose_calc = qta_netta / volume_teorico if volume_teorico > 0 else 0
            else:
                dose_calc = qta_netta / ettari if ettari and ettari > 0 else 0

            dose_round = round(dose_calc, 1)
            if min_s and min_s > 0 and dose_round < round(min_s, 4):
                testo_msg = f"⚠️ Dose Bassa su {codice}: Calcolata {dose_round} {um_str} per «{prodotto}» (minimo etichetta: {min_s})."
                avvisi_per_trattamento.setdefault(t_id, []).append(testo_msg)
            elif max_s and max_s > 0 and dose_round > round(max_s, 4):
                testo_msg = f"⚠️ Dose Eccessiva su {codice}: Calcolata {dose_round} {um_str} per «{prodotto}» (massimo etichetta: {max_s})."
                avvisi_per_trattamento.setdefault(t_id, []).append(testo_msg)

        # 3. Controllo Intervallo Minimo
        intervallo_viol = conn.execute(text("""
            SELECT t.id, ten.codice, p.nome_prodotto, p.intervallo_min_tratt,
                   (
                       SELECT t2.data_trattamento
                       FROM trattamenti t2
                       JOIN dettaglio_trattamenti dt2 ON t2.id = dt2.trattamento_id
                       WHERE t2.prodotto_id = t.prodotto_id
                         AND dt2.tendone_id = dt.tendone_id
                         AND (dt2.is_bilanciamento = 0 OR dt2.is_bilanciamento IS NULL)
                         AND (t2.data_trattamento < t.data_trattamento
                              OR (t2.data_trattamento = t.data_trattamento AND t2.id < t.id))
                       ORDER BY t2.data_trattamento DESC, t2.id DESC
                       LIMIT 1
                   ) AS d_prec,
                   t.data_trattamento
            FROM dettaglio_trattamenti dt
            JOIN trattamenti t ON t.id = dt.trattamento_id
            JOIN tendoni ten ON ten.id = dt.tendone_id
            JOIN prodotti p ON p.id = t.prodotto_id
            WHERE p.intervallo_min_tratt IS NOT NULL AND p.intervallo_min_tratt > 0
              AND (dt.is_bilanciamento = 0 OR dt.is_bilanciamento IS NULL)
        """)).fetchall()

        for t_id, codice, prodotto, intervallo, d_prec, d_att in intervallo_viol:
            if d_prec:
                def _p(d):
                    return datetime.strptime(d, '%Y-%m-%d').date() if isinstance(d, str) else d
                giorni_passati = (_p(d_att) - _p(d_prec)).days
                if giorni_passati < intervallo:
                    testo_msg = f"⚠️ Tendone {codice}: Intervallo minimo violato ({giorni_passati} gg su {intervallo} richiesti per «{prodotto}»)."
                    avvisi_per_trattamento.setdefault(t_id, []).append(testo_msg)

        # 4. Scrittura finale nel Database (batchata in una sola executemany)
        payloads = [
            {"id": t_id, "t": "\n".join(dict.fromkeys(avvisi))}
            for t_id, avvisi in avvisi_per_trattamento.items()
        ]
        if payloads:
            conn.execute(
                text("INSERT INTO avvisi_trattamenti (trattamento_id, testo) VALUES (:id, :t)"),
                payloads,
            )

    except Exception as e:
        log.warning("Errore durante il ricalcolo degli avvisi: %s", e)
