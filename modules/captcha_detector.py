import re
from modules.logger import logger

async def detect_captcha_or_login_wall(page) -> tuple[bool, str]:
    """
    Checks the active page for CAPTCHAs or Login/Account creation walls.
    Returns (True, reason) if detected, otherwise (False, "").
    Works with both raw Playwright Page objects and browser-use Page wrappers.
    """
    # Extract raw Playwright page if browser-use wraps it
    p_page = page
    if hasattr(page, 'page'):
        p_page = page.page
    elif hasattr(page, 'get_playwright_page'):
        p_page = page.get_playwright_page()
    elif hasattr(page, '_page'):
        p_page = page._page

    # --- 1. CAPTCHA iframe detection ---
    # Invisible/badge-mode reCAPTCHA (size=invisible) runs silently in the
    # background on a huge share of legitimate application forms and never
    # blocks a real user - only an escalated challenge frame (the image-grid
    # "bframe", or a visibly rendered checkbox widget) actually needs a human.
    try:
        frames = p_page.frames
        for frame in frames:
            url = frame.url.lower()

            if "recaptcha" in url or "hcaptcha" in url:
                if "bframe" in url or "/challenge" in url:
                    # Escalated challenge frame - always blocking regardless of anchor mode
                    return "Human Attention", f"CAPTCHA challenge iframe detected: {url}"
                if "size=invisible" in url:
                    # Runs silently, doesn't present anything to the user - not blocking
                    continue
                # Visible-mode checkbox widget - only blocking if actually rendered visibly
                try:
                    frame_elem = await frame.frame_element()
                    if frame_elem and await frame_elem.is_visible():
                        return "Human Attention", f"CAPTCHA iframe detected: {url}"
                    continue
                except Exception:
                    return "Human Attention", f"CAPTCHA iframe detected: {url}"

            if "cloudflare" in url or "challenges.cloudflare.com" in url or "turnstile" in url:
                return "Human Attention", f"Cloudflare Turnstile / challenge iframe detected: {url}"

            if "arkoselabs" in url or "funcaptcha" in url:
                return "Human Attention", f"Arkose / FunCaptcha iframe detected: {url}"

            if "datadome" in url or "captcha-delivery.com" in url:
                return "Human Attention", f"DataDome CAPTCHA iframe detected: {url}"

            if "awswaf" in url or "token.awswaf.com" in url:
                return "Human Attention", f"AWS WAF CAPTCHA iframe detected: {url}"

            if "geetest" in url:
                return "Human Attention", f"GeeTest CAPTCHA iframe detected: {url}"
    except Exception as e:
        logger.debug(f"Error checking frames: {e}")

    # --- 2. Element/class/id CAPTCHA detection ---
    captcha_selectors = [
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
    ]
    
    for selector in captcha_selectors:
        try:
            elements = await p_page.locator(selector).all()
            for elem in elements:
                if await elem.is_visible():
                    return "Human Attention", f"CAPTCHA element detected: {selector}"
        except Exception as e:
            logger.debug(f"Error checking selector {selector}: {e}")

    # --- 3. Page body text CAPTCHA / Bot check detection ---
    try:
        body_text = await p_page.inner_text("body")
        body_text_lower = body_text.lower()
        captcha_text_patterns = [
            "verify you are human",
            "please verify you are a human",
            "complete the security check",
            "security check to continue",
            "we want to make sure you're a human",
            "bot verification",
            "checking your browser",
            "press & hold to confirm you are human",
            "press and hold to confirm you are human"
        ]
        for pattern in captcha_text_patterns:
            if pattern in body_text_lower:
                return "Human Attention", f"CAPTCHA text pattern detected: '{pattern}'"
    except Exception as e:
        logger.debug(f"Error checking body text: {e}")

    # --- 3.5 OTP / Verification Code Detection ---
    try:
        otp_text_patterns = [
            "verification code was sent",
            "verification code has been sent",
            "a verification code was sent",
            "enter the 8-character code",
            "enter the 6-digit code",
            "code to confirm you're a human",
            "code to confirm you are a human",
            "enter the verification code",
            "enter verification code",
            "enter the security code",
            "enter security code",
            "one-time password",
            "one-time passcode",
            "one-time verification code",
            "security code"
        ]
        for frame in p_page.frames:
            try:
                # Check rendered text
                frame_text = (await frame.inner_text("body")).lower()
                for pattern in otp_text_patterns:
                    if pattern in frame_text:
                        return "OTP Required", f"OTP / verification code prompt detected: '{pattern}'"

                # Check headings and labels
                headings = await frame.locator("h1, h2, h3, h4, label, [role='heading']").all_inner_texts()
                for h in headings:
                    h_lower = h.lower().strip()
                    if any(kw in h_lower for kw in ["security code", "verification code", "enter code"]):
                        return "OTP Required", f"OTP / verification code heading detected: '{h}'"
            except Exception:
                pass

        otp_selectors = [
            "[autocomplete='one-time-code']",
            "[data-automation-id*='verificationCode']",
            "[data-automation-id*='securityCode']",
            "[data-automation-id*='otp']",
            "[data-qa*='security-code']",
            "[data-qa*='verification-code']",
            "label:has-text('Security code')",
            "label:has-text('Security Code')",
            "label:has-text('Verification code')",
            "[class*='security-code']",
            "[class*='verification-code']",
            "[id*='security_code']",
            "[id*='verification_code']",
            "[id*='security-code']",
            "[id*='verification-code']",
            "input[name*='verification_code']",
            "input[name*='security_code']",
            "input[name*='otp']",
            "input[aria-label*='security code' i]",
            "input[aria-label*='verification code' i]"
        ]
        for frame in p_page.frames:
            for selector in otp_selectors:
                try:
                    elements = await frame.locator(selector).all()
                    for elem in elements:
                        if await elem.is_visible():
                            return "OTP Required", f"OTP / verification code input detected: {selector}"
                except Exception as e:
                    logger.debug(f"Error checking OTP selector {selector}: {e}")
    except Exception as e:
        logger.debug(f"Error checking OTP detection: {e}")

    # --- 4. Login / Account Creation wall detection ---
    login_wall_selectors = [
        "input[type='password']",
        "form[action*='login']",
        "form[action*='signin']",
        "form[action*='signup']",
        "form[action*='register']",
        "[data-automation-id='signInSubmitButton']",
        "[data-automation-id='createAccountSubmitButton']",
        "button:has-text('Create Account')",
        "button:has-text('Sign In')",
        "button:has-text('Log In')"
    ]
    
    for frame in p_page.frames:
        for selector in login_wall_selectors:
            try:
                elements = await frame.locator(selector).all()
                for elem in elements:
                    if await elem.is_visible():
                        return "Sign In Required", f"Login/signup wall element: {selector}"
            except Exception as e:
                logger.debug(f"Error checking login selector {selector}: {e}")

    try:
        # Check for headings or buttons indicating login/registration wall
        for frame in p_page.frames:
            headings = await frame.locator("h1, h2, h3, h4, [role='heading']").all_inner_texts()
            login_terms = [
                "sign in", "log in", "create account", "create an account",
                "register to apply", "sign up", "password requirements",
                "verify new password", "already have an account"
            ]
            for heading in headings:
                heading_lower = heading.lower().strip()
                if any(term in heading_lower for term in login_terms):
                    return "Sign In Required", f"Login/signup wall heading detected: '{heading}'"
    except Exception as e:
        logger.debug(f"Error checking headings: {e}")

    return None, ""

