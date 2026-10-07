# Running the logger on a cloud server

This guide moves the logger from your laptop to a small always-on server at DigitalOcean, so paper
trading and everything else run 24/7. Nothing changes about what it records. It is still read-only,
and there is no Kalshi key and no trading.

**Cost:**
- DigitalOcean: **$6 a month** to start, then $12 a month once the disk fills (part 9). On the $6 plan
  the disk lasts roughly 6-11 months at the current data rate.
- Backblaze backups: about $0-0.20 a month (the first 10 GB are free).

**Time:** about an hour, mostly waiting. You never type commands: every step is a double-click,
except pasting a few things into websites.

**Exact wording on websites may differ slightly from this guide.** Look for the closest match.

---

## Before you start

1. **Merge the server pull request on GitHub.** The server downloads the code from `main`, so it
   needs the merged version.
2. **Update your laptop copy.** In GitHub Desktop, click *Fetch origin*, then *Pull*. You should now
   see files such as `make_server_key.bat` and `setup_server.bat` in the project folder.
3. **Keep the laptop logger running for now.** You'll stop it in part 5, and not before.

---

## Part 1: make your login key (2 minutes)

Double-click **`make_server_key.bat`**.

- It creates a key pair in your Windows user folder (`.ssh\kalshi_server`). This is what lets your
  laptop log in to the server instead of a password.
- The **public** half is copied to your clipboard. It's safe to paste into DigitalOcean.
- The **private** half never leaves your laptop. Don't share or upload it.

If it says the SSH client wasn't found, see "If something goes wrong" at the end.

---

## Part 2: DigitalOcean account and server (15 minutes)

### Sign up and protect the account

1. Go to **digitalocean.com** and sign up. You add a payment card during signup.
2. **Turn on two-factor login.** Click your account icon (top right), then *My Account* (or
   *Settings*), then *Security*, then set up two-factor authentication. This account is your way back
   in if you ever lose your laptop (see "Locked out"), so it's worth protecting.

### Set a billing alert at $15 a month

1. Open **Billing**: the account icon (top right), then *Billing*, or *Settings*, then *Billing*.
2. Find **Billing alerts** and click *Set alert* (or *Edit*).
3. Enter **15** (dollars) and save.

DigitalOcean emails you if the month's charges pass $15. The expected bill is $6, or $12 after the
resize.

### Create the server

1. Click **Create**, then **Droplets**.
2. **Region:** *New York*. Any New York datacenter is fine.
3. **Image:** *Ubuntu*, version **24.04 (LTS) x64**.
4. **Size:** *Basic*, then *Regular* (SSD), then the **$6/mo** option (1 GB memory, 25 GB disk).
5. **Authentication:**
   1. Choose **SSH Key**, then *New SSH Key*.
   2. Click in the big box and press **Ctrl+V** to paste the key from part 1. It starts with
      `ssh-ed25519`.
   3. Name it `kalshi laptop` and click *Add SSH Key*.
   4. Make sure it is ticked.
6. **Leave paid backups off.** Backups go to Backblaze instead (part 4).
7. **Hostname:** `kalshi-logger`.
8. Click **Create Droplet**.

### Save the server's address

1. After a minute the server shows an **IP address** (four numbers with dots, e.g. 203.0.113.25).
   Click it to copy it.
2. Double-click **`set_server_address.bat`**, paste the address (right-click pastes), and press Enter.

---

## Part 3: set up the server (10-15 minutes)

1. Double-click **`setup_server.bat`**. It logs in to the server and does everything in one go:
   - **Clock:** sets it to **Pacific time**, so daily and weekly reports split at the same times as
     on your laptop.
   - **Updates:** installs the latest security updates.
   - **Login:** turns off password login entirely, so only your key from part 1 works.
   - **Firewall:** turns it on with **only SSH open**.
   - **Automatic updates:** turns on automatic security updates. When an update needs a restart, the
     server restarts itself at **4 a.m. Pacific**, and the logger restarts with it.
   - **Memory:** adds 2 GB of swap, so the occasional big report fits in 1 GB of memory.
   - **Logger service:** installs the logger and checks it, then sets it up as a service that starts
     on boot and restarts itself within 30 seconds if it ever crashes.
   - **Backup timer:** schedules the daily 5 a.m. backup.
2. **Partway through, it pauses and shows a line starting with `ssh-ed25519`.** That is the server's
   *read-only* GitHub key. It lets the server download this project's code and nothing else. Add it on
   GitHub:
   1. github.com, then **Arax01/kalshi-logger**, then **Settings**, then **Deploy keys**, then
      **Add deploy key**.
   2. **Title:** `kalshi server`.
   3. **Key:** select the whole `ssh-ed25519 ...` line in the black window (drag across it with the
      mouse; selecting copies it), then paste it here.
   4. Leave **"Allow write access" unticked.**
   5. Click *Add key*.
   6. Go back to the black window and press **Enter**.
3. It finishes with **"Server setup finished."** By then it has also run the tests and checked that
   Kalshi's public data can be read from the server.

**What it doesn't do yet:** the logger does **not** start yet. It waits for your database (part 5),
so nothing is ever logged twice.

If anything stops it, read the last lines it printed. **It's always safe to run `setup_server.bat`
again.**

---

## Part 4: daily backups to Backblaze B2 (15 minutes)

### Create the account and bucket

1. Go to **backblaze.com**, choose **B2 Cloud Storage**, and sign up. The first 10 GB are free. Add a
   payment card when asked; the expected charge is $0-0.20 a month.
2. **Buckets**, then **Create a Bucket**:
   - **Name:** something unique, e.g. `kalshi-logger-backup-` plus a few random letters.
   - **Files in Bucket:** *Private*.
   - **Encryption:** either setting is fine (the backups are already encrypted on the server).
   - **Object Lock:** *Disable*.
   - Click *Create a Bucket*.
3. On the new bucket, click **Lifecycle Settings**, choose **"Keep only the last version of the
   file"**, and save. Without this, old backups are never really deleted and you'd pay to keep them.

### Create a key for the server

1. Go to **Application Keys**, then **Add a New Application Key**:
   - **Name:** `kalshi-server`.
   - **Allow access to Bucket(s):** only your new bucket.
   - **Type of Access:** *Read and Write*.
   - Click *Create New Key*.
2. Copy the **keyID** and the **applicationKey** somewhere safe right away, e.g. your password
   manager. Backblaze shows the applicationKey only once.
3. **What this key can do:** read and write backups in that one bucket. It can't touch anything else
   in your Backblaze account. It lives only on the server.

### Choose a backup password

1. Make a long password in your password manager and label it "Kalshi backup password".
2. **Keep it safe. If it's lost, the backups can't be opened by anyone, including you.**

### Connect the server

1. Double-click **`setup_backups.bat`** and paste in, when asked:
   - the bucket name;
   - the keyID;
   - the applicationKey;
   - the backup password, twice.
2. The hidden fields show nothing as you type or paste; that's normal. Right-click pastes.
3. It checks the connection and runs a first backup, which is tiny until your data moves in part 5.

**How the backups work:**
- **When:** a backup runs every day at **5 a.m. Pacific**.
- **What's uploaded:** only what changed, compressed (about 10× smaller) and encrypted.
- **What's kept:** 7 daily, 4 weekly and 12 monthly backups.
- **Checking on it:** `server_status.bat` shows when the last one ran and whether it worked.

---

## Part 5: move your data (20-40 minutes, depending on laptop upload speed)

This copies your laptop's database to the server **once**. After it, the laptop logger never runs
again, so there are never two loggers.

**Do these in this order, without a break:**

1. **Check the server is ready.** Double-click `server_status.bat`. It should say *"No database on the
   server yet"*. If `setup_server.bat` hasn't finished successfully, go back to part 3.
2. **Stop the laptop logger now.** Double-click **`stop.bat`** and wait 30 seconds. This is the moment
   the laptop stops for good.
3. **Copy the database.** Double-click **`move_to_server.bat`**. It:
   1. refuses to continue if the laptop logger is still running;
   2. checks the database is intact and makes one complete copy;
   3. counts the rows in every table;
   4. uploads the copy;
   5. on the server, checks the upload is byte-for-byte identical (checksum) and has exactly the same
      row counts. If anything differs, it changes nothing and tells you;
   6. puts it in place **only if the server has no database yet**, so it can never overwrite data the
      server has collected;
   7. starts the logger on the server and shows its status.
4. **Remove the laptop's startup shortcut.**
   1. Press `Windows key + R`, type `shell:startup`, and press Enter.
   2. Delete the shortcut to `start.bat`.
   3. As a safety net, `start.bat` now refuses to run anyway: `move_to_server.bat` leaves a file called
      `MOVED_TO_SERVER` in the project folder.

**If the upload is interrupted**, just double-click `move_to_server.bat` again. Don't restart the
laptop logger in between.

**Nothing is lost or duplicated:**
- **One writer:** there is only ever one logger writing data.
- **The switch:** the 10-30 minutes between `stop.bat` and the server starting are recorded as a data
  gap, like any time the laptop was off.
- **Paper orders:** open paper orders carry on, on the server. Their fills during the switch are read
  from Kalshi's trade history afterwards, so none are missed.
- **Your laptop copy:** the laptop's own database stays where it is, untouched, as an extra copy.

About 30 minutes later, run `server_status.bat` again. You should see scans completed, crypto fair
values and paper orders placed, with no errors.

---

## Everyday use (all double-clicks on your laptop)

| File | What it does |
|---|---|
| `server_status.bat` | Is the logger running, what it collected in the last 24 hours, data gaps, disk space (with a warning at 70%), last backup, and automatic restarts. |
| `server_report.bat` | Writes any due reports on the server, plus a preview of the latest data, then downloads all reports to your laptop's `reports` folder and opens it. |
| `get_reports.bat` | Just downloads the latest reports and opens the folder. The daily and weekly reports are written automatically on the server. |
| `update_server.bat` | After you merge a pull request: updates the server (see below). |
| `restore_backup.bat` | Restores the database from a backup (see below). |

The old `start.bat`, `stop.bat`, `status.bat` and `report.bat` are for the laptop logger, which is
retired.

---

## Updates: after you merge a pull request

1. On GitHub, merge the pull request as usual.
2. In GitHub Desktop, *Fetch origin*, then *Pull*. This keeps the `.bat` files on your laptop
   current.
3. Double-click **`update_server.bat`**. It:
   1. downloads the new version on the server;
   2. runs the tests there;
   3. restarts the logger on the new version only if the tests pass;
   4. shows the status.

If the tests fail, it puts the old version back and keeps running it, and says so. Tell Claude what
it printed. If it says the update **changes server settings**, double-click `setup_server.bat` once
as well; it's safe to run again.

---

## Backups and restoring

**Restoring on the same server** (for example if the database ever gets damaged):

1. Double-click **`restore_backup.bat`**.
2. It lists the backups by date. Type the ID of the one you want, or press Enter for the newest.
3. Type **YES** to go ahead. It then:
   - stops the logger;
   - rebuilds the database from the backup and checks it;
   - sets the old database aside, so it isn't deleted;
   - starts the logger again.
4. **About disk space:**
   - If there isn't room to keep the old one, it asks you to type DELETE first.
   - Once you're happy with the restore, delete the old copy to free the space. Ask Claude how; it's
     one command.

**What you lose:** anything collected after the backup you chose (at most about a day with the
newest), recorded as a gap. A large database takes about a minute per GB to restore.

**If the whole server is lost** (deleted by mistake, or the account closed):
1. Create a new server (part 2).
2. Run `set_server_address.bat` with the new address.
3. Run `setup_server.bat` (part 3).
4. Run `setup_backups.bat` with the **same** bucket, keyID, applicationKey and backup password. It
   says it found your existing backups.
5. Run `restore_backup.bat`. The logger starts as soon as the restore finishes.

**Extra copy:** your laptop's old database (from before the move) is an extra, older copy.

---

## When the disk warning appears: resize to the $12 plan

`server_status.bat` warns when the disk is over 70% full. Then:

1. In DigitalOcean, open the droplet `kalshi-logger`.
2. Click **Power**, then **Turn off**. The logger stops cleanly, and the downtime shows as a gap.
3. Click **Resize**:
   1. Choose **CPU, RAM and Disk** (not "CPU and RAM only").
   2. Pick **Basic, Regular, $12/mo** (2 GB memory, 50 GB disk).
   3. Click *Resize*. This is permanent: the disk can't be made smaller again, and that's fine.
4. Click **Power**, then **Turn on**. The disk grows automatically and the logger starts by itself.
5. Run `server_status.bat` to check. Disk use should now be about half what it was.

With 50 GB the data should fit for about another year or more.

---

## Locked out: lost laptop or lost key

The way back in is your **DigitalOcean account** (password and two-factor). You don't need the old
laptop.

### 1. Get the project on the new (or same) laptop

1. Install Python and GitHub Desktop as in the README's one-time setup, and clone the project.
2. Double-click `setup.bat`.
3. Double-click `make_server_key.bat`. It makes a new key and copies its public half to the
   clipboard.
4. Double-click `set_server_address.bat` and enter the server's IP address. It's on the droplet's page
   in DigitalOcean.

### 2. Open the server's console in your browser

1. Log in to DigitalOcean and open the droplet `kalshi-logger`.
2. Click **Access**, then **Launch Droplet Console**. A black terminal opens in the browser, logged in
   as `root`. This works even though password login is turned off.

### 3. Let the new key in

1. Type `kalshi-add-key --replace '` (ending with a single quote).
2. Paste your new public key: right-click and choose Paste, or press Ctrl+Shift+V.
3. Type another `'`.
4. Press Enter. The whole line looks like:

   ```
   kalshi-add-key --replace 'ssh-ed25519 AAAA...your key... kalshi-logger laptop key'
   ```

**Which form to use:**
- `--replace` removes every old key, so the lost laptop can't log in any more. Use it whenever a
  laptop was lost or stolen.
- To *add* a second laptop and keep the first, leave out `--replace`.

The command prints the keys that can now log in. Close the console. The `.bat` files work again from
the new laptop.

### If the Droplet Console won't open

1. On the droplet's **Access** page, click **Reset root password**. DigitalOcean turns the server off
   and on, and emails you a temporary root password.
2. Click **Launch Recovery Console** (also on the Access page). Log in as `root` with that password.
   You'll be asked to choose a new one.
3. Run the same `kalshi-add-key` line as above. Typing the whole key by hand is error-prone, so if
   pasting doesn't work in this console, ask Claude for help.

**About that root password:** it only works in DigitalOcean's console. Logging in over the internet
still needs a key.

### What a lost laptop exposes

- **Only its server login key**, which `--replace` cancels.
- **Not the backups:** the Backblaze key and backup password live only on the server and in your
  password manager.
- **Not GitHub:** the server's GitHub key is read-only and lives only on the server.
- **No Kalshi keys:** none exist.

---

## Security in short

- **Login:** SSH keys only. Password login is off for everyone.
- **Firewall:** everything closed except SSH.
- **Updates:** automatic security updates, with automatic restarts at 4 a.m. Pacific when needed.
- **Logger:** runs as its own user (`kalshi`), not as the administrator. That user can only start,
  stop and restart the logger, and run a backup.
- **Keys on the server:**
  - the read-only GitHub deploy key (this one repository);
  - the Backblaze key (one bucket).
  - Neither can place trades, and there is no Kalshi key at all.

---

## If something goes wrong

- **"SSH client wasn't found".**
  1. Windows 10/11 include it, but it can be switched off. Open *Settings*, then *System* (or *Apps*),
     then *Optional features*.
  2. Look for **OpenSSH Client**. If it's missing, click *Add a feature* (or *View features*), add
     it, and restart the laptop.
- **"REMOTE HOST IDENTIFICATION HAS CHANGED"** after you created a new server at the same address:
  delete the file `kalshi_known_hosts` in the `.ssh` folder of your Windows user folder, and try
  again.
- **"Permission denied (publickey)":**
  - Check the address with `set_server_address.bat`.
  - If this laptop's key isn't on the server, see "Locked out".
- **Anything else:** run `server_status.bat` and send Claude what it shows. The logger's own log is
  on the server in `kalshi-logger/logs/logger.log`; Claude can tell you how to view it.
