# London grad jobs (Vercel edition)

Checks company job boards and UK job sites for new graduate roles in London.
Lets you save roles, mark applications and hide the ones you don't want.
Runs on Vercel, so it works from any phone or laptop.

Everything here fits in free plans: Vercel Hobby (personal, non-commercial
use), a Neon Postgres database, and GitHub Actions for hourly checks.

## 1. Put the code on GitHub

1. Create a free account at github.com, then **New repository**. Make it
   **Private**, and give it a name like `london-grad-jobs`.
2. On the empty repository page, click **uploading an existing file** and drag
   in everything from this folder.
3. Check that `.github/workflows/check-jobs.yml` came through. Macs hide
   folders starting with a dot, so if it's missing: **Add file → Create new
   file**, type `.github/workflows/check-jobs.yml` as the name, and paste in
   the file's contents.

## 2. Deploy on Vercel

1. Sign up at vercel.com with your GitHub account.
2. **Add New → Project**, import the repository, and click **Deploy**. Vercel
   recognises it as a Flask app; no settings to change. The first deploy will
   show an error page. That's expected until steps 3 and 4 are done.
3. In the project, open the **Storage** tab, create a **Postgres** database
   (Neon), and connect it to this project. This adds `DATABASE_URL` for you.
4. **Settings → Environment Variables**, add:
   - `APP_PASSWORD`: the password for the site.
   - `CRON_SECRET`: a long random string (30+ characters; a password
     generator is ideal). Scheduled checks use it to prove they're allowed.
5. **Deployments**, open the menu on the latest one, and click **Redeploy**
   so the new settings take effect.

## 3. First use

1. Open your app's address (shown in Vercel, like `https://london-grad-jobs.vercel.app`).
2. The browser asks for a username and password. The username can be
   anything; the password is `APP_PASSWORD`. Browsers remember it.
3. Open **Settings**, add companies and job-site keys, then **Save and check now**.

## 4. Hourly checks

Vercel's free plan only allows one scheduled run a day. That's set up already
(6am UTC, in `vercel.json`) but grad schemes move faster than that, so GitHub
Actions calls the app every hour instead:

1. In the GitHub repository: **Settings → Secrets and variables → Actions →
   New repository secret**. Add two:
   - `APP_URL`: your app's address, with no slash at the end.
   - `CRON_SECRET`: exactly the same value as in Vercel.
2. **Actions** tab → **Check for new grad jobs** → **Run workflow** to test it.
   A green tick means it worked.

GitHub may run scheduled jobs a few minutes late at busy times. To change how
often it checks, edit the `cron:` line in `.github/workflows/check-jobs.yml`.

## Good to know

- A check has about 4 minutes before it stops (Vercel's limit is 5). The
  "Last check" box in Settings says if any companies were skipped for time.
- Add companies by the name in their job page address:
  boards.greenhouse.io/**name**, jobs.lever.co/**name**,
  jobs.ashbyhq.com/**name**, jobs.smartrecruiters.com/**name**.
- Email alerts: Settings → tick the box. For Gmail, use an App Password
  (Google Account → Security → App passwords), not the normal password.
