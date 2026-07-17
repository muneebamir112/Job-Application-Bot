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
    try:
        frames = p_page.frames
        for frame in frames:
            url = frame.url.lower()
            if any(term in url for term in ["recaptcha", "hcaptcha", "cloudflare", "challenges.cloudflare.com"]):
                return True, f"CAPTCHA iframe detected: {url}"
    except Exception as e:
        logger.debug(f"Error checking frames: {e}")

    # --- 2. Element/class/id CAPTCHA detection ---
    captcha_selectors = [
        "[class*='recaptcha']", "[id*='recaptcha']",
        "[class*='hcaptcha']", "[id*='hcaptcha']",
        "[class*='cf-challenge']", "[id*='cf-challenge']",
        "[id*='captcha']", "[class*='captcha']",
        "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
        "iframe[src*='cloudflare']",
        "[role='checkbox'][aria-label*='human']",
        "[role='checkbox'][aria-label*='captcha']",
        "[aria-label*='verify you are human']",
        "[aria-label*='verify page']"
    ]
    
    for selector in captcha_selectors:
        try:
            elements = await p_page.locator(selector).all()
            for elem in elements:
                if await elem.is_visible():
                    return True, f"CAPTCHA element detected: {selector}"
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
            "checking your browser"
        ]
        for pattern in captcha_text_patterns:
            if pattern in body_text_lower:
                return True, f"CAPTCHA text pattern detected: '{pattern}'"
    except Exception as e:
        logger.debug(f"Error checking body text: {e}")

    # --- 4. Login / Account Creation wall detection ---
    login_wall_selectors = [
        "input[type='password']",
        "form[action*='login']",
        "form[action*='signin']",
        "form[action*='signup']"
    ]
    
    for selector in login_wall_selectors:
        try:
            elements = await p_page.locator(selector).all()
            for elem in elements:
                if await elem.is_visible():
                    # Double check if this is indeed a login form rather than a field
                    # For password fields, it's almost always a login/signup blocker
                    if selector == "input[type='password']":
                        return True, "Login/signup wall: password input visible"
                    return True, f"Login/signup wall element: {selector}"
        except Exception as e:
            logger.debug(f"Error checking login selector {selector}: {e}")

    try:
        # Check for headings or buttons indicating login/registration wall
        headings = await p_page.locator("h1, h2, h3").all_inner_texts()
        login_terms = ["sign in to your account", "log in to your account", "create an account", "register to apply", "sign up to continue"]
        for heading in headings:
            heading_lower = heading.lower()
            if any(term in heading_lower for term in login_terms):
                return True, f"Login/signup wall heading detected: '{heading}'"
    except Exception as e:
        logger.debug(f"Error checking headings: {e}")

    return False, ""
