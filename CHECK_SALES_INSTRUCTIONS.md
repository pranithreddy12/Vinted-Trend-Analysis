# Checking confirmed sales for your products

This shows, for each product you choose, how many sales Vinted has **confirmed** and at what listing price.
It only reads the files your tracker already writes, so nothing is sent anywhere.

## One-time setup (5 minutes)

1. Copy the new files I sent into your Vinted folder, **replacing** the old ones with the same name:
   `fetch_results.py`, `track_sales.py`, `check_model_sales.py`, `check_models.bat`, `model_checks.txt`.
2. Do not delete the `tracking` folder. It holds your history.
3. Let the tracker run as normal. **Wait for one full cycle (about 24 hours).**
   The Smeg history is cleaned during its first run: the kettles, fridges and other non-bottle
   listings are moved to `tracking\smeg_tumbler_offtype_archive.csv`. Nothing is lost.

## Running the check (30 seconds)

1. Double-click **`check_models.bat`** in your Vinted folder.
2. A window opens and shows two blocks: the last **48 hours** and the last **7 days**.
3. For each product you will see:
   - **tracked**: listings found for this exact product
   - **active**: still for sale now
   - **confirmed sold**: Vinted's own item page shows "Sold"
   - **listing price**: median and range of the asking price at the time of sale
   - one line per sale: date, price, variant, title
4. Press any key to close the window.
5. Each run adds lines to **`model_checks_log.csv`** in the same folder. **Please send me that file** after the
   48-hour check and again after the 7-day check, then weekly. It records how sales evolve.

## Dates

- Start counting 48 hours from the moment the tracker has finished one full cycle with the new files.
- Run the check at 48 hours, at 7 days, then once a week.

## Following more products

Open `model_checks.txt` in Notepad. Each line is one product, fields separated by `;`:

```
name ; tracking file ; brand ; colour ; capacity ; line
Stanley Quencher H2.0 40oz (1.18L) pink ; tracking/stanley_*.csv ; stanley ; pink ; 40oz ; Quencher
Smeg cream insulated bottle 500ml ; tracking/smeg_tumbler.csv ; smeg ; cream ; 0.5l ;
```

Copy a line, change the words, save. The last two fields (capacity, line) can stay empty.
Capacity is written as `40oz` or `0.5l` (500 ml = `0.5l`, 600 ml = `0.6l`).

## Things to know

- **Price**: Vinted does not publish the price actually paid. The tool shows the **listing price** at the time of sale.
- **Small products**: for a niche product such as the Smeg 500 ml bottle, 48 hours may show only a few sales
  (or none). The 7-day view is the first one that means something.
- **Colour "pink"** also covers rose and Rose Quartz. A few special editions or bundles can appear; tell me if you want them excluded.
- **A number looks wrong?** Send me `model_checks_log.csv` and the file in the `tracking` folder for that product.
