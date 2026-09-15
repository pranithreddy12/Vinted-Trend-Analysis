# Vision AI Setup — Windows (your dedicated machine)

This walks you through connecting your Anthropic API key to the tracker, safely, on your own
Windows PC. Follow it in order — each step confirms the previous one worked before you spend
anything.

**Do this ONLY on the Windows machine that actually runs the automation** (the dedicated one), not
on the freelancer's machine.

---

## 0. Before you start

- [ ] You have an Anthropic API key (starts with `sk-ant-...`)
- [ ] **Run `setup.bat`** (double-click it, or run it from Command Prompt in the `src` folder)
      — it installs everything in one go, including the vision AI packages, so you don't need
      to remember a separate `pip install` step. If you already ran the tracker before this
      guide existed, run `setup.bat` anyway — it's safe to run again, it just re-confirms
      everything's installed.
- [ ] **Do not** paste your API key into any chat, email, or file that gets shared/committed
      anywhere. It only ever goes into this machine's own environment variables (Step 2).

---

## 1. Set a spend cap in the Anthropic Console (do this first, before anything else)

1. Go to **console.anthropic.com** and log in with the account tied to your key.
2. Find **Billing → Usage limits** (or "Spend limits" — the exact wording can change).
3. Set a monthly cap — **$20** to start, as agreed.

This is your real safety net — even if something in the setup goes wrong, spend physically cannot
exceed this number.

---

## 2. Set the API key as a Windows environment variable

Do **not** put the key inside any `.bat`/`.py` file. Set it once at the system level:

1. Press the **Windows key**, type `environment variables`, open
   **"Edit the system environment variables"**.
2. Click **Environment Variables...**
3. Under **User variables** (top box), click **New...**
4. Variable name: `ANTHROPIC_API_KEY`
   Variable value: your key (paste it here, nowhere else)
5. Click OK on all the dialogs to save.

**Verify it saved** — open a **new** Command Prompt window (must be new, existing ones won't see
it) and run:
```
echo %ANTHROPIC_API_KEY%
```
It should print your key back. If it prints nothing, redo step 2 — most common mistake is closing
the old terminal instead of opening a fresh one.

---

## 3. Set the AI model (cheaper than the default, on purpose)

The tracker defaults to the most powerful (and most expensive) model. For "identify this product
from a photo," a cheaper model does the job for a fraction of the cost.

1. In **console.anthropic.com**, check which models are available on your account/plan.
2. Using the same Environment Variables screen as Step 2, add another **New...** user variable:
   Variable name: `VINTED_VISION_MODEL`
   Variable value: the cheaper model's exact name from the console (e.g. a Sonnet or Haiku tier —
   copy the exact string shown in the console, don't guess it)
3. Save, then verify the same way as Step 2:
   ```
   echo %VINTED_VISION_MODEL%
   ```

If you're not sure which model name to use, send a screenshot of the console's model list and
we'll confirm together before you spend anything.

---

## 4. Turn the feature on

Open `run_tracker.bat` in Notepad and find these two lines (currently commented out with `REM`):
```
REM set VINTED_VISION=1
REM set VINTED_VISION_PROVIDER=anthropic
REM set VINTED_DEDUP=1
```
Remove `REM ` (the word and the space after it) from all three lines so they read:
```
set VINTED_VISION=1
set VINTED_VISION_PROVIDER=anthropic
set VINTED_DEDUP=1
```
`VINTED_DEDUP=1` matters — it's what makes an already-identified product **free** to reuse the
next time a different seller lists the same thing, instead of paying again. Save the file.

**Hold off running it yet — see the note below.**

---

## 5. First real run

The offers-targeting logic is now built and pushed: the AI only identifies listings that already
show real buyer interest — **4 or more offers** — instead of a broad sweep of every new listing
(configurable via `VINTED_VISION_MIN_OFFERS` if you ever want to raise/lower that 4).

Run `run_tracker.bat` once manually (don't wait for the schedule) and check the output for a line
like:
```
🔎 product identification: 6/40 titled (offers≥4) → ... (provider: anthropic)
```
That confirms it's using the real AI (not the free stub) and shows how many listings actually got
identified that run — a small number is expected and correct, since only offers≥4 listings qualify.

---

## 6. Checking actual cost

After the first few runs, check **console.anthropic.com → Usage** to see real spend so far. Send
us a screenshot after the first day — we'll use it to sanity-check that the $20 budget will
comfortably last the 2-week test, and adjust the model or threshold if needed.
