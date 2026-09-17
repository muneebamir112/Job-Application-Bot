import asyncio
from patchright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://cognizant.taleo.net/careersection/lateral/jobdetail.ftl?job=00070542192&lang=en", wait_until="load")
        
        # Give it a bit to render
        await asyncio.sleep(5)
        
        # Dump all elements containing 'Apply'
        print("Elements containing 'Apply':")
        locators = await page.locator("text=/Apply/i").all()
        for i, loc in enumerate(locators):
            try:
                tag = await loc.evaluate("el => el.tagName")
                text = await loc.evaluate("el => el.innerText")
                id_attr = await loc.evaluate("el => el.id")
                class_attr = await loc.evaluate("el => el.className")
                value_attr = await loc.evaluate("el => el.value")
                is_vis = await loc.is_visible()
                print(f"[{i}] {tag} (id='{id_attr}', class='{class_attr}', value='{value_attr}') visible={is_vis}")
            except Exception as e:
                print(f"[{i}] Error: {e}")
                
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
