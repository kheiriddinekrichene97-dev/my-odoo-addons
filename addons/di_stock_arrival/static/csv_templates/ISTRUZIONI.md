# Modelli per gli Arrivi Merce

Due modelli da compilare prima dell'import in Odoo
(**Arrivi Merce → apri un arrivo in Bozza → Importa da CSV**):

| File | Quando usarlo |
|---|---|
| `modello_arrivo.csv` | Registra solo quello che è arrivato. La divisione fra i negozi si fa dopo, in Odoo. |
| `modello_arrivo_con_divisione.csv` | La merce è già stata smistata: ogni riga dice anche quanti pezzi vanno a ogni destinazione. |

## Come compilarli

1. Apri il file con Excel o LibreOffice (è UTF-8 con separatore `;`).
2. **Cancella le due righe di esempio `ESEMPIO-001` / `ESEMPIO-002`** e inserisci
   le tue righe al loro posto.
3. Salva in formato **CSV UTF-8 (delimitato da punto e virgola)** oppure
   **Cartella di lavoro Excel (.xlsx)**: Odoo accetta entrambi.
4. Se salvi in CSV, nel wizard di import lascia Separatore = `Semicolon ;` e
   Codifica = `UTF-8`. Con un file `.xlsx` queste due opzioni spariscono: non
   servono.

Di un file `.xlsx` viene letto **solo il primo foglio**, e la sua prima riga
deve essere l'intestazione. Il vecchio formato `.xls` non è supportato: aprilo
e risalvalo come `.xlsx` o come CSV.

Non rinominare né riordinare le colonne che non stai usando: una colonna
lasciata vuota viene semplicemente ignorata, una colonna rinominata no.

## Colonne

| Colonna | Obbligatoria | Significato |
|---|---|---|
| `codice` | **sì** | Riferimento interno dell'articolo (il codice ESC) |
| `quantita` | **sì** | Pezzi arrivati. `0` va bene: la riga entra lo stesso, con zero pezzi |
| `nome` | solo per articoli nuovi | Descrizione, serve per creare l'articolo se non esiste |
| `colore` | se l'articolo ha varianti | Es. `Nero`, `Blu` |
| `taglia` | se l'articolo ha varianti | Es. `M`, `L`, `42` |
| `prezzo_acquisto` | no | Costo (listino ESC 4 ACQUISTO) |
| `prezzo_dettaglio` | no | Prezzo di vendita + listino **Dettaglio** (ESC 2 VENDITA) |
| `prezzo_promo` | no | Listini **Promo** e **Promo Franchising** (ESC 1 VENDITA SCONTATO) |
| `prezzo_ingrosso` | no | Listino **Ingrosso** (ESC 3 VENDITA INGROSSO) |
| `categoria` | no | Categoria prodotto |
| `marca`, `stagione`, `reparto`, `posizionamento` | no | Classificazione |

Al posto di `nome` puoi intestare la colonna `descrizione`: è la stessa cosa.

Una riga per **variante**: stesso `codice`, un rigo per ogni combinazione di
colore e taglia. Se l'articolo ha una sola variante, `colore` e `taglia` si
lasciano vuoti.

I decimali si scrivono con la virgola (`12,50`).

I **codici a barre non si importano**: li genera Odoo. Quelli del fornitore si
scartano per policy.

## Colonne di divisione (solo nel secondo modello)

Le colonne `PTREM`, `PTF`, `MTREM` sono **esempi**: l'intestazione deve
corrispondere al **codice breve** (o al nome completo) del magazzino in Odoo.
Rinomina, aggiungi o togli colonne in base alle destinazioni che ti servono.

- Non mettere una colonna per il magazzino di arrivo: quello che resta lì non
  va dichiarato, si calcola da solo.
- La somma delle destinazioni può essere **minore** della quantità arrivata: la
  differenza resta al magazzino centrale. È normale, non è un errore.
- Una cella vuota o `0` significa "niente a questa destinazione".

Nell'esempio: dei 10 pezzi di `ESEMPIO-001` ne partono 4 + 3 + 2 = 9, e 1 resta
al magazzino di arrivo.

## Cosa succede alle righe sbagliate

Una riga senza codice, con quantità non numerica o con un articolo non
trovato viene **saltata**, non blocca l'import, e viene riepilogata nel
messaggio in fondo all'arrivo (chatter), raggruppata per motivo. Correggi il
file e reimportalo: le righe già presenti vengono sommate, non duplicate.

**La quantità `0` è ammessa**: la riga viene importata con zero pezzi, così
l'arrivo mostra tutta la griglia taglie che il fornitore ha mandato, comprese
le taglie non arrivate. Una cella vuota vale `0`. Nessun pezzo entra in
magazzino per quelle righe; se una taglia arriva dopo, correggi la quantità
sull'arrivo finché non è ancora diviso e la merce entra da sola.

Viene invece saltata la riga con una **quantità negativa**, o con una
quantità che non è un numero.
