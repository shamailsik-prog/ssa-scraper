# Mirroring the corpus to Google Drive

The archive mirror copies every promoted judgment (original PDF or rendered copy, full text,
metadata), statute and instrument to each enabled archive target, write-once, with `_index` CSVs.
Until a Google Drive target is registered, the server keeps copies on its own disk only
(`local`, `local_export`).

## Which credential

* **Personal Drive (a gmail.com account)** — use an OAuth grant from that account
  (`client_id`, `client_secret`, `refresh_token`). Files count against the account's own storage.
  A service account cannot be used here: it has no storage of its own, and Google refuses its
  uploads into a personal My Drive (`storageQuotaExceeded`).
* **Google Workspace Shared Drive** — a service account added to the Shared Drive as a Content
  manager (`service_account_json`) also works.

## One-time setup for a personal Drive (browser only, about 15 minutes)

1. **Google Cloud project.** At <https://console.cloud.google.com/>, signed in as the Drive's owner,
   create a project (any name). *APIs & Services → Library*: enable **Google Drive API**.
2. **Consent screen.** *APIs & Services → OAuth consent screen*: User type **External**, app name
   of your choice, your address as support and developer contact. Add yourself under *Test users*,
   then press **Publish app** so the status reads *In production*. In *Testing* status Google
   expires the grant after 7 days and the mirror would stop. Google does not need to verify an app
   that only its owner uses: you click through its "unverified app" warning once, in step 4.
3. **OAuth client.** *APIs & Services → Credentials → Create credentials → OAuth client ID*:
   type **Web application**, authorised redirect URI
   `https://developers.google.com/oauthplayground`. Copy the **Client ID** and **Client secret**.
4. **Refresh token.** Open <https://developers.google.com/oauthplayground>. Press the gear icon,
   tick *Use your own OAuth credentials* and paste the Client ID and secret. In *Step 1* type the
   scope `https://www.googleapis.com/auth/drive`, press *Authorize APIs* and sign in as the Drive's
   owner (*Advanced → Go to … (unsafe)* on the unverified-app page, then *Allow*). In *Step 2* press
   *Exchange authorization code for tokens* and copy the **Refresh token**.
5. **GitHub secrets.** In this repository, *Settings → Secrets and variables → Actions*: add
   `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET` and `GDRIVE_REFRESH_TOKEN`.
6. **Folder.** In Google Drive create a folder for the corpus and open it. The address ends in
   `/folders/<folder id>`; copy that id.
7. **Connect.** *Actions → connect-google-drive → Run workflow*, paste the folder id. The run
   registers the target on the server (credentials encrypted at rest, never echoed), creates
   `SIKANDER_Corpus/_index` in the folder and lists it. A green run means the server can write
   there. The hourly *archive-mirror* task then fills it, backlog first.
8. **Check.** *Actions → server-logs → Run workflow* with services `status`: the *archive targets*
   section shows `google_drive` with objects written, failures and the last error.

`mirror_pakistanlawsite` (default `true`) decides whether PakistanLawSite judgments are copied to the
Drive as well as the public statutes. Set it to `false` if the subscription terms do not allow
copies outside the firm's server.

Re-running the workflow with new secrets updates the same target in place.
