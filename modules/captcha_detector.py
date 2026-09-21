import asyncio
import re
from modules.logger import logger

async def detect_captcha_or_login_wall(page) -> tuple[str | None, str]:
    """
    Checks the active page for CAPTCHAs or Login/Account creation walls.
    Returns (status, reason) if detected, otherwise (None, "").
    Uses 100% non-blocking URL inspection and high-speed in-page DOM evaluation (<5ms).
    Never calls frame.frame_element() to guarantee zero Playwright CDP hangs.
    """
    # Extract raw Playwright page if browser-use wraps it
    p_page = page
    if hasattr(page, 'page'):
        p_page = page.page
    elif hasattr(page, 'get_playwright_page'):
        p_page = page.get_playwright_page()
    elif hasattr(page, '_page'):
        p_page = page._page

    # --- 1. Instantaneous CAPTCHA challenge frame URL check (0ms, zero CDP calls) ---
    try:
        frames = p_page.frames
        for frame in frames:
            url = frame.url.lower()

            # Active interactive puzzle challenges:
            if ("recaptcha" in url or "hcaptcha" in url) and ("bframe" in url or "/challenge" in url or "userverify" in url):
                return "Human Attention", f"CAPTCHA challenge iframe detected: {url}"

            if "challenges.cloudflare.com" in url or ("cloudflare" in url and "turnstile" in url):
                return "Human Attention", f"Cloudflare Turnstile challenge detected: {url}"

            if "arkoselabs" in url or "funcaptcha" in url:
                return "Human Attention", f"Arkose / FunCaptcha challenge detected: {url}"

            if "datadome" in url or "captcha-delivery.com" in url:
                return "Human Attention", f"DataDome CAPTCHA detected: {url}"

            if "awswaf" in url or "token.awswaf.com" in url:
                return "Human Attention", f"AWS WAF challenge detected: {url}"

            if "geetest" in url:
                return "Human Attention", f"GeeTest CAPTCHA detected: {url}"
    except Exception as e:
        logger.debug(f"Error checking frames: {e}")

    # --- 2. High-speed In-Page DOM evaluation (<5ms) ---
    try:
        res = await asyncio.wait_for(p_page.evaluate("""() => {
            // 1. Check body text for CAPTCHA patterns
            const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
            const captchaPatterns = [
                "verify you are human", "please verify you are a human",
                "complete the security check", "security check to continue",
                "we want to make sure you're a human", "bot verification",
                "checking your browser", "press & hold to confirm you are human",
                "press and hold to confirm you are human"
            ];
            for (const pat of captchaPatterns) {
                if (bodyText.includes(pat)) return ["Human Attention", `CAPTCHA text pattern detected: '${pat}'`];
            }

            // 2. Check CAPTCHA DOM elements
            const captchaSelectors = [
                "[class*='recaptcha']", "[id*='recaptcha']",
                "[class*='hcaptcha']", "[id*='hcaptcha']",
                "[class*='cf-challenge']", "[id*='cf-challenge']",
                ".cf-turnstile", "[data-turnstile-sitekey]", "[name='cf-turnstile-response']",
                "[id*='captcha']", "[class*='captcha']",
                "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
                "iframe[src*='cloudflare']", "iframe[src*='turnstile']",
                "iframe[src*='arkoselabs']", "[id*='fc-iframe-wrap']",
                "[role='checkbox'][aria-label*='human' i]",
                "[role='checkbox'][aria-label*='captcha' i]",
                "[aria-label*='verify you are human' i]",
                "[aria-label*='verify page' i]",
                "#px-captcha", ".px-captcha", "[id*='datadome']"
            ];
            for (const sel of captchaSelectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        // Must be visible (non-zero size)
                        if (el.offsetWidth === 0 || el.offsetHeight === 0) continue;

                        // Skip hidden reCAPTCHA v3 token inputs / badge elements:
                        // - input[type='hidden'] are never user-visible challenges
                        // - Elements with opacity:0, visibility:hidden, or display:none
                        // - Elements positioned far offscreen (left < -100 or top < -100)
                        if (el.tagName === 'INPUT' && (el.type === 'hidden' || el.type === 'text' && el.style.display === 'none')) continue;
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.left < -100 || rect.top < -100) continue;
                        // reCAPTCHA v3 badge is bottom-right corner — small (< 70px wide) and not a challenge
                        // Only skip if it has 'grecaptcha-badge' class specifically (the floating badge)
                        if (el.classList.contains('grecaptcha-badge')) continue;

                        return ["Human Attention", `CAPTCHA element detected: ${sel}`];
                    }
                } catch(e) {}
            }

            // 3. Check OTP prompts and inputs
            const otpPatterns = [
                "verification code was sent", "verification code has been sent",
                "a verification code was sent", "enter the 8-character code",
                "enter the 6-digit code", "code to confirm you're a human",
                "code to confirm you are a human", "enter the verification code",
                "enter verification code", "enter the security code",
                "enter security code", "one-time password", "one-time passcode",
                "one-time verification code"
            ];
            for (const pat of otpPatterns) {
                if (bodyText.includes(pat)) return ["OTP Required", `OTP / verification code prompt detected: '${pat}'`];
            }

            const otpSelectors = [
                "[autocomplete='one-time-code']",
                "[data-automation-id*='verificationCode']",
                "[data-automation-id*='securityCode']",
                "[data-automation-id*='otp']",
                "[data-qa*='security-code']",
                "[data-qa*='verification-code']",
                "[class*='security-code']",
                "[class*='verification-code']",
                "[id*='security_code']",
                "[id*='verification_code']",
                "[id*='security-code']",
                "[id*='verification-code']",
                "input[name*='verification_code']",
                "input[name*='security_code']",
                "input[name*='otp']"
            ];
            for (const sel of otpSelectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        if (el.offsetWidth > 0 && el.offsetHeight > 0) {
                            return ["OTP Required", `OTP / verification code input detected: ${sel}`];
                        }
                    }
                } catch(e) {}
            }

            // 4. Check Login / Account creation wall
            const loginSelectors = [
                "input[type='password']",
                "form[action*='login']",
                "form[action*='signin']",
                "form[action*='signup']",
                "form[action*='register']",
                "[data-automation-id='signInSubmitButton']",
                "[data-automation-id='createAccountSubmitButton']"
            ];
            for (const sel of loginSelectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        if (el.offsetWidth > 0 && el.offsetHeight > 0) {
                            return ["Sign In Required", `Login/signup wall element: ${sel}`];
                        }
                    }
                } catch(e) {}
            }

            // Check headings for login terms
            const headings = document.querySelectorAll("h1, h2, h3, h4, [role='heading']");
            const loginTerms = [
                "sign in", "log in", "create account", "create an account",
                "register to apply", "sign up", "password requirements",
                "verify new password", "already have an account"
            ];
            for (const h of headings) {
                const txt = (h.innerText || '').toLowerCase().trim();
                if (loginTerms.some(t => txt === t || txt.startsWith(t))) {
                    return ["Sign In Required", `Login/signup wall heading detected: '${h.innerText}'`];
                }
            }

            return [null, ""];
        }"""), timeout=1.0)
        if res and res[0]:
            return res[0], res[1]
    except Exception as e:
        logger.debug(f"Error during JS evaluation for captcha/login wall: {e}")

    return None, ""
