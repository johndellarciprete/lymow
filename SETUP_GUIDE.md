# Lymow for Homey — Complete Setup & Pairing Guide

A start-to-finish guide for someone **new to Homey and the Homey CLI**. You'll download the app
from GitHub, install the tools it needs, put it on your Homey, and pair a mower. Written for
**Windows**; Mac/Linux notes are called out where they differ.

> This app lives at **<https://github.com/johndellarciprete/lymow>**. The folder it downloads into
> is called the **app folder** below.

---

## 1. What you need (requirements)

**Hardware / accounts**
- A **Homey Pro (2023 or newer)** on firmware **13.0.0 or later**. (This app runs locally in
  Python, which needs a Homey Pro — not the older Homey, and not Homey Cloud.)
- A **computer** (Windows/Mac/Linux) on the **same Wi‑Fi/local network** as the Homey. This is
  required to install the app; after install the app runs on the Homey by itself.
- A **Lymow account** (email + password, or Google) that already has your mower set up in the
  official Lymow phone app.

**Software you'll install (details in Section 2)**
| Software | Why | Needed for |
|---|---|---|
| Git | Download the app from GitHub (and run helper scripts) | Always |
| Node.js (LTS) | Runs the Homey CLI | Always |
| Homey CLI (`homey`) | Talks to your Homey | Always |
| Docker Desktop | Builds the app's Python parts | Always (must be **running**) |
| Python 3 + Playwright | The Google sign-in helper | Only if you pair with **Google** |

---

## 2. Install the dependencies (one time)

### 2a. Git
1. Install **Git for Windows** from <https://git-scm.com/download/win> (accept defaults). This also
   gives you **Git Bash**, used by a helper script later.
   *(Mac: `brew install git` or Xcode tools. Linux: `sudo apt install git`.)*
2. Verify in **PowerShell**:
   ```powershell
   git --version
   ```

### 2b. Node.js
1. Download the **LTS** installer from <https://nodejs.org> and run it (accept defaults).
2. Verify:
   ```powershell
   node --version
   npm --version
   ```

### 2c. Homey CLI
```powershell
npm install -g homey
```
Verify, then sign in to your Athom (Homey) account — this opens a browser:
```powershell
homey --version
homey login
```

### 2d. Docker Desktop
The app contains Python code that Homey compiles inside Docker, so Docker must be installed
**and running**.
1. Download Docker Desktop from <https://www.docker.com/products/docker-desktop/> and install it.
2. **Launch Docker Desktop** and wait until it says "Engine running" (steady whale icon in the
   tray). Leave it running while you install the app.
3. Verify:
   ```powershell
   docker version
   ```

### 2e. Python 3 + Playwright — *only if pairing with Google*
Email/password pairing does **not** need this; skip if you use email/password.
1. Install **Python 3** from <https://www.python.org/downloads/> — during setup, tick
   **"Add python.exe to PATH"**.
2. Install the Playwright library (the helper uses your already‑installed Chrome or Edge, so no
   extra browser download is needed):
   ```powershell
   pip install playwright
   ```
3. Verify:
   ```powershell
   python --version
   ```

---

## 3. Download the app from GitHub

Pick a folder to keep it in (this example uses your user folder) and clone the repo:

```powershell
cd $HOME
git clone https://github.com/johndellarciprete/lymow.git
cd lymow
```

That last folder — where you now are — is the **app folder**. Run all remaining commands from here.

> **No Git / prefer a ZIP?** On the GitHub page click **Code → Download ZIP**, extract it, then
> `cd` into the extracted folder. You'll still need Git Bash (Section 2a) for the Windows fix in
> Troubleshooting.

---

## 4. Prepare the app (one time)

From inside the app folder, build the app's Python dependencies (uses Docker — make sure Docker
Desktop is running):
```powershell
homey app dependencies install
```
> A fresh download from GitHub does **not** include the compiled Python libraries — this step
> builds them. If a later step complains about a *"cross-compiled virtual environment"*, see
> Troubleshooting (Section 7).

---

## 5. Put the app on your Homey

1. See your Homeys and pick the right one:
   ```powershell
   homey list
   homey select
   ```
   (Or target one directly: `homey select --id <the-id-from-the-list>`.)

2. Install the app permanently (must be on the **same network** as that Homey):
   ```powershell
   homey app install
   ```
   Success looks like: `Homey App '...lymow' successfully installed`.

The app now runs on the Homey on its own — you can close PowerShell and the computer.

---

## 6. Pair the mower

Pairing happens in the **Homey app** (phone app, or **my.homey.app** in a computer browser).

1. Open the Homey app → **Devices** → **＋ (Add device)** → **Lymow** → **Lymow Mower**.
2. Choose the **region** your Lymow account was created in (e.g. US East).
3. Choose a sign-in method:

### Option A — Email & Password (easiest)
- Enter your Lymow email and password → the app lists your mower(s) → tap your mower to add it.
- Done.

### Option B — Google
Google can't hand the sign-in code back automatically, so a small helper captures it.
1. In the pairing screen, choose **Google** → **Continue with Google** → tap **Copy link**.
   Leave the Homey screen open.
2. On your computer, from the **app folder**, in PowerShell:
   ```powershell
   python scripts/google_code_helper.py "PASTE_THE_SIGN-IN_LINK_HERE"
   ```
   (Keep the quotes.) A Chrome window opens — **sign in with Google** as normal.
3. When sign-in finishes, the helper **prints an authorization code right in the PowerShell
   (terminal) window**, like this:
   ```
   AUTHORIZATION CODE (paste into Homey, then press Verify):

      1a2b3c4d-5e6f-7890-abcd-ef1234567890
   ```
   Select and copy that code from the terminal.
4. Switch back to the **Homey app window** (the pairing screen you left open) and **paste the code**
   into the "Paste the callback URL (or just the code)" field → tap **Verify** → tap your mower to
   add it.

> In short: the **sign-in link** goes into the terminal (Step 2); the **code** the terminal prints
> goes back into the Homey app (Step 4).
>
> ⏱️ The code expires in about **60 seconds** — have the Homey app window ready and paste promptly.
> Always use a **fresh** "Copy link" for each attempt.

Once added, the mower appears as a device with battery, status, sensors, control buttons
(Start / Pause / Dock / Stop + drive arrows), a Mow Zone picker, and settings.

---

## 7. Optional: the drive Dashboard (hold-to-drive)

1. In the Homey app, open or create a **Dashboard**.
2. **Add widget → Lymow Drive** → open its settings → **pick your mower**.
3. You get a D-pad (▲ ◄ STOP ► ▼) and a deck-height slider. **Press and hold** an arrow to drive
   continuously; release to stop.

The mower must be **awake and off the dock** to accept manual driving.

---

## 8. Troubleshooting

- **`homey app install` says the Homey is offline** — you're not on the same local network as
  that Homey. Join its Wi‑Fi and retry.
- **Error: "collecting cross-compiled virtual environment"** (Windows) — the Docker-built Python
  folders contain Linux-only symlinks Windows can't copy. From the app folder, fix once with Git
  Bash:
  ```bash
  bash scripts/fix_windows_venv.sh
  ```
  then run `homey app install` again.
- **Docker errors / "Is Docker running?"** — open Docker Desktop and wait for the engine to start,
  then retry.
- **Google: "this browser may not be secure"** — the helper is designed to use your *normal*
  Chrome/Edge to avoid this. Make sure Chrome or Edge is installed. Re-run the helper.
- **Google: nothing happens when I paste the link into Homey** — the sign-in **link** goes into the
  **PowerShell helper**, not into Homey. Only the **code** the helper prints goes into Homey.
- **No devices found after sign-in** — wrong region, or the account has no mower. Re-check the
  region and that the mower is set up in the official Lymow app.
- **To update the app later** — from the app folder, `git pull` to get the newest files, then
  repeat Section 5 (`homey select` the right Homey, then `homey app install`) while on that Homey's
  network.

---

## Notes on how it works
- **Commands, status, and settings** go through Lymow's cloud, so they work from anywhere once the
  app is installed and the mower is online.
- **Local-only features** (e.g. the mower's onboard camera) need Homey and the mower on the same
  network; that's why remote live video isn't included.
- Device pairings live on **one Homey** — if you install on a second Homey, pair the mower there
  too.
