// ENG-152 avatar smoke (light, deliberately narrow): over the REAL stack —
// upload a profile picture in the ProfileDialog, see the dialog render the
// image, see the sidebar footer UserCard + a sent message's avatar chip switch
// from initials to the image (the directory fold + workspace-readable serve
// endpoint working end to end), then remove the photo and see initials return.

import { expect, test, type Page } from '@playwright/test'

// Matches serverctl.py's bootstrap identity.
const OWNER = {
  email: 'owner@example.com',
  password: 'correct-horse-battery-staple',
}

/** A tiny valid 2×2 red PNG (the server re-encodes it to its 256×256 WEBP). */
const PNG_BYTES = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFElEQVR4nGP8z8DwnwEKmBhQAAMAHxcCAmHc/ooAAAAASUVORK5CYII=',
  'base64',
)

async function login(page: Page): Promise<void> {
  await page.goto('/login')
  await page.fill('[data-test="email"]', OWNER.email)
  await page.fill('[data-test="password"]', OWNER.password)
  await page.click('[data-test="submit"]')
  await expect(page.locator('[data-testid="sidebar-channel"]').first()).toBeVisible({
    timeout: 30_000,
  })
}

test('avatar: upload in profile → image renders in dialog, footer + messages → remove restores initials', async ({
  browser,
}) => {
  const ctx = await browser.newContext()
  const page = await ctx.newPage()
  await login(page)

  // Open the profile dialog from the sidebar footer card.
  await page.click('[data-testid="user-card"]')
  const avatar = page.locator('[data-testid="profile-avatar"]')
  await expect(avatar).toBeVisible({ timeout: 10_000 })
  await expect(avatar).toHaveAttribute('data-has-avatar', 'false')

  // Upload the PNG through the hidden picker; the dialog previews/serves the image.
  await page.setInputFiles('[data-testid="profile-avatar-upload"]', {
    name: 'face.png',
    mimeType: 'image/png',
    buffer: PNG_BYTES,
  })
  await expect(avatar).toHaveAttribute('data-has-avatar', 'true', { timeout: 20_000 })
  await expect(page.locator('[data-testid="profile-avatar-remove"]')).toBeVisible()

  // Send a message so a message row exists to render the author avatar.
  await page.click('[data-testid="profile-close"]')
  await page.locator('[data-testid="sidebar-channel"]').first().click()
  const text = `avatar smoke ${Date.now()}`
  await page.fill('[data-testid="composer-input"]', text)
  await page.click('[data-testid="composer-send"]')
  await expect(page.locator('[data-testid="message-text"]', { hasText: text })).toBeVisible({
    timeout: 20_000,
  })

  // Reload so the directory fold rebuilds from the SYNCED user.profile_updated
  // (deterministic — no dependence on the post-upload refresh landing before the
  // event syncs). The footer card + the message chip now render the avatar IMAGE,
  // fetched end to end through the workspace-readable serve endpoint.
  await page.reload()
  await page.locator('[data-testid="sidebar-channel"]').first().click()
  await expect(
    page.locator('[data-testid="user-card"] [data-has-image="true"]').first(),
  ).toBeVisible({ timeout: 20_000 })
  await expect(
    page.locator('[data-testid="message-avatar"][data-has-image="true"]').first(),
  ).toBeVisible({ timeout: 20_000 })

  // Remove the photo → initials come back in the dialog.
  await page.click('[data-testid="user-card"]')
  await page.click('[data-testid="profile-avatar-remove"]')
  await expect(page.locator('[data-testid="profile-avatar"]')).toHaveAttribute(
    'data-has-avatar',
    'false',
    { timeout: 20_000 },
  )

  await ctx.close()
})
