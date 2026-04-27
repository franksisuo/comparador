import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import Stealth  # <- nuevo

async def run():
    try:
        async with async_playwright() as p:
            # 1. Lanzar con argumentos que ocultan automatización
            browser = await p.chromium.launch(
                headless=True,
                slow_mo=0,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                ]
                )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="es-CL",
                timezone_id="America/Santiago",
            )
            await Stealth().apply_stealth_async(context)

            page = await context.new_page()
            # Aplica parches stealth (desactiva webdriver, etc.)

            # Parche adicional por si el stealth no cubre todo
            await context.add_init_script('''() => {
                delete navigator.__proto__.webdriver;
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                window.chrome = { runtime: {} };
            }''')

            print("1. Accediendo a Easy...")
            await page.goto("https://www.easy.cl/", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(500)

            # ----- LIMPIEZA AGRESIVA DE MODALES -----
            print("2. Cerrando overlays y banners...")
            for _ in range(3):
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(200)
            await page.evaluate('''() => {
                const selectors = [
                    '[data-testid="coachmark-popover"]',
                    '[data-testid="overlay"]',
                    '.SearchBar-module_coachmark-wrapper',
                    '.modal', '.Modal', '.overlay', '.backdrop',
                    '#onesignal-slidedown-container',
                    'div[class*="cookie"]', 'div[id*="cookie"]',
                    'div[class*="geofinder"]',
                ];
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                });
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            }''')

            # ----- GEOLOCALIZACIÓN -----
            print("3. Configurando Talca, Maule...")
            try:
                await page.screenshot(path="before_geo.png")
                geo_btn = page.locator('button').filter(has=page.locator('strong').filter(has_text="Talca"))
                await geo_btn.wait_for(state="visible", timeout=5000)
                await geo_btn.click(force=True)
                await page.wait_for_timeout(1000)
                await page.screenshot(path="after_click_geo.png")

                reg_input = page.locator('input[placeholder*="Ingresar"]').first
                await reg_input.wait_for(state="visible", timeout=5000)
                await reg_input.fill("Talca, Maule")
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(500)
                print("   ✓ Ubicación configurada a Talca, Maule")
            except Exception as e:
                print(f"⚠️ No se pudo configurar ubicación: {e}")
                print("   Continuando sin ubicación específica...")

            # ----- BÚSQUEDA HÍBRIDA -----
            print("4. Buscando producto...")
            search_input = page.locator(
                'input[name="q"], #search-input'
            ).first
            await search_input.wait_for(state="visible", timeout=5000)

            await search_input.click(force=True)
            await page.wait_for_timeout(200)
            await search_input.fill("")  # limpiar

            texto = "Perfil Cuadrado Acero 75x75x2 mm 6 m"  # Cambiar producto si es necesario
            await search_input.fill(texto)
            # Asegurar que React detecte el cambio
            await search_input.evaluate(f'(el) => {{ el.value = "{texto}"; el.dispatchEvent(new Event("input", {{ bubbles: true }})); }}')
            await page.wait_for_timeout(100)

            valor = await search_input.input_value()
            if valor.strip() == texto:
                print("   ✓ Texto ingresado correctamente.")
            else:
                print(f"   ⚠️ Texto en el input: '{valor}'")
                await search_input.evaluate(f'(el) => {{ el.value = "{texto}"; el.dispatchEvent(new Event("input", {{ bubbles: true }})); }}')
                await page.wait_for_timeout(200)

            await search_input.press("Enter")
            print("URL after search:", page.url)

            # ----- CERRAR POPUP DESPUÉS DE BÚSQUEDA -----
            await page.wait_for_timeout(500)  # Esperar que aparezca el popup
            try:
                await page.locator('button[data-testid="popover-button"]:has-text("Entendido")').click(timeout=2000)
                print("5. Popup de búsqueda por imagen cerrado.")
            except:
                print("   ⚠️ No se encontró popup para cerrar.")

            try:
                await page.wait_for_url("**/search**", timeout=5000)  # Adaptar URL pattern para Easy
                await page.wait_for_load_state('networkidle')  # Esperar que cargue completamente
                await page.wait_for_timeout(2000)  # Esperar un poco más
                print("5. Búsqueda lanzada con éxito. URL:", page.url)
                await page.screenshot(path="resultados_easy.png")
            except:
                print("   ⚠️ URL no cambió, verificando resultados...")
                await page.wait_for_timeout(1000)

            # Extraer precio usando evaluate para buscar texto con precio
            count = await page.locator('text=/\\$/').count()  # Buscar texto con $
            print(f"   Número de elementos con precio: {count}")
            try:
                price_element = page.locator('text=/\\$ [0-9]{2,}/').first
                full_text = await price_element.text_content()
                import re
                match = re.search(r'\\$ [0-9.,]+', full_text)
                if match:
                    precio = match.group(0)
                    print(f"   ✓ Precio encontrado: {precio}")
                else:
                    print(f"   ⚠️ Texto encontrado: {full_text}")
            except Exception as e:
                print(f"   ⚠️ Error extrayendo precio: {e}")
    finally:
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())