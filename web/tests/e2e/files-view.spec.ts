// ENG-152 Files view smoke (bonus, light): upload an attachment into #general,
// open the sidebar's Files section (`nav-files`), and see the workspace file
// listing render the uploaded file with its metadata + a download action. Drives
// the same REAL production stack as the other e2e specs (serverctl.py harness);
// deliberately a smoke — the view's behavior matrix lives in FilesView.spec.ts.

import { expect, test, type Page } from '@playwright/test'

// Matches serverctl.py's bootstrap identities.
const OWNER = { email: 'owner@example.com', password: 'correct-horse-battery-staple' }

const WS_TIMEOUT = 20_000

/** Log in and land on the authed shell (sidebar populated). */
async function login(page: Page, who: { email: string; password: string }): Promise<void> {
  await page.goto('/login')
  await page.fill('[data-test="email"]', who.email)
  await page.fill('[data-test="password"]', who.password)
  await page.click('[data-test="submit"]')
  await expect(page.locator('[data-testid="sidebar-channel"]').first()).toBeVisible({
    timeout: 30_000,
  })
}

test('files view: upload → open Files → the workspace listing shows the file + download', async ({
  browser,
}) => {
  const ctx = await browser.newContext()
  const page = await ctx.newPage()
  await login(page, OWNER)

  // Open #general and send a message carrying one attachment.
  await page.locator('[data-testid="sidebar-channel"]').first().click()
  await expect(page.getByTestId('message-list')).toBeVisible()
  const content = `files-view smoke ${Date.now()}`
  await page.getByTestId('attach-file-input').setInputFiles({
    name: 'files-view-smoke.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from(content, 'utf-8'),
  })
  await expect(page.locator('[data-testid="composer-attachment"][data-phase="done"]')).toBeVisible({
    timeout: WS_TIMEOUT,
  })
  await page.getByTestId('composer-input').click()
  await page.getByTestId('composer-input').fill('here is the smoke file')
  await page.getByTestId('composer-send').click()
  await expect(page.getByTestId('message-list').getByTestId('attachment-file').first()).toBeVisible(
    { timeout: WS_TIMEOUT },
  )

  // Flip to the Files section: the listing shows the uploaded file's row with
  // its name, the uploader, the source channel, and a download affordance.
  await page.getByTestId('nav-files').click()
  await expect(page.getByTestId('files-view')).toBeVisible()
  const row = page
    .getByTestId('files-list')
    .getByTestId('file-row')
    .filter({ hasText: 'files-view-smoke.txt' })
  await expect(row).toBeVisible({ timeout: WS_TIMEOUT })
  await expect(row.getByTestId('file-channel')).toContainText('general')
  await expect(row.getByTestId('file-download')).toBeVisible()

  // Download round-trips the exact uploaded bytes through the worker blob path.
  const downloadPromise = page.waitForEvent('download')
  await row.getByTestId('file-download').click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('files-view-smoke.txt')

  await ctx.close()
})
