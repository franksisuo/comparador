import asyncio
import re
import unicodedata
from urllib.parse import quote, urljoin
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
]

CONTEXT_KWARGS = {
    "viewport": {"width": 1280, "height": 800},
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "locale": "es-CL",
    "timezone_id": "America/Santiago",
}

REGION_OBJETIVO = "Maule"
COMUNA_OBJETIVO = "Talca"
COMUNA_SODIMAC = "Talca - Talca"
NAV_TIMEOUT = 12000
ELEMENT_TIMEOUT = 3500
RESULT_TIMEOUT = 4500
SHORT_WAIT_MS = 180
RETRY_WAIT_MS = 300


def _formatear_precio(raw):
    if raw is None:
        return None
    if isinstance(raw, int):
        return f"${raw:,}".replace(",", ".")
    texto = str(raw).strip()
    numeros = "".join(ch for ch in texto if ch.isdigit())
    if not numeros:
        return None
    return f"${int(numeros):,}".replace(",", ".")


def _normalizar(texto):
    base = unicodedata.normalize("NFD", texto.lower())
    limpio = "".join(ch for ch in base if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", limpio).strip()


def _keywords_producto(producto):
    tokens = re.split(r"[^a-zA-Z0-9x]+", _normalizar(producto))
    return [t for t in tokens if len(t) >= 3 or any(ch.isdigit() for ch in t)]


def _elegir_mejor_link(producto, candidatos):
    keywords = _keywords_producto(producto)
    mejor = None
    mejor_score = -1
    for item in candidatos:
        texto = _normalizar(item.get("text", ""))
        href = (item.get("href") or "").strip()
        if not href:
            continue
        score = 0
        for kw in keywords:
            if kw in texto:
                score += 1
            if kw in href.lower():
                score += 1
        if score > mejor_score:
            mejor = href
            mejor_score = score
    return mejor if mejor_score > 0 else None


def _href_util(href):
    if not href:
        return False
    h = href.strip().lower()
    if len(h) < 5:
        return False
    if h.startswith(("javascript:", "#", "mailto:")):
        return False
    if h in {"p", "/p"}:
        return False
    return True


def _dimension_objetivo(producto):
    texto = _normalizar(producto)
    m = re.search(r"(\d+\s*x\s*\d+\s*x\s*\d+)", texto)
    if not m:
        return None
    return re.sub(r"\s+", "", m.group(1))


def _coincide_dimension(texto, dimension):
    if not dimension:
        return True
    base = re.sub(r"[^a-z0-9x]", "", _normalizar(texto))
    objetivo = re.sub(r"[^a-z0-9x]", "", dimension.lower())
    return objetivo in base


async def _bloquear_recursos(context):
    async def handler(route):
        req = route.request
        if req.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", handler)


async def _crear_contexto(browser):
    context = await browser.new_context(**CONTEXT_KWARGS)
    await Stealth().apply_stealth_async(context)
    await _bloquear_recursos(context)
    await context.add_init_script(
        """() => {
            delete navigator.__proto__.webdriver;
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {} };
        }"""
    )
    return context


async def _limpiar_modales(page):
    for _ in range(1):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(80)
    await page.evaluate(
        """() => {
            const selectors = [
                '[data-testid="coachmark-popover"]',
                '[data-testid="overlay"]',
                '.SearchBar-module_coachmark-wrapper',
                '.modal', '.Modal', '.overlay', '.backdrop',
                '#onesignal-slidedown-container',
                'div[class*="cookie"]', 'div[id*="cookie"]',
                'div[class*="geofinder"]'
            ];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => el.remove());
            });
            document.body.style.overflow = 'auto';
            document.documentElement.style.overflow = 'auto';
        }"""
    )


async def _configurar_ubicacion_easy(page):
    try:
        actual = await page.evaluate(
            """() => {
                const el = document.querySelector("span.select-address");
                return (el?.textContent || "").trim();
            }"""
        )
        if COMUNA_OBJETIVO.lower() in actual.lower():
            return
    except Exception:
        pass

    try:
        await page.get_by_role("button", name=re.compile("ubic", re.I)).first.click(
            timeout=ELEMENT_TIMEOUT,
            force=True,
        )
        await page.wait_for_timeout(SHORT_WAIT_MS)
        await page.locator("#region").fill(REGION_OBJETIVO, timeout=ELEMENT_TIMEOUT)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(SHORT_WAIT_MS)
        await page.locator("#comuna").fill(COMUNA_OBJETIVO, timeout=ELEMENT_TIMEOUT)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(SHORT_WAIT_MS)
        await page.get_by_role("button", name=re.compile("guardar", re.I)).first.click(
            timeout=ELEMENT_TIMEOUT,
            force=True,
        )
        await page.wait_for_timeout(SHORT_WAIT_MS)
    except Exception:
        pass


async def _configurar_ubicacion_sodimac(page):
    try:
        actual = await page.evaluate(
            """() => document.body.innerText.includes('Talca - Talca')"""
        )
        if actual:
            return
    except Exception:
        pass

    try:
        await page.evaluate("document.querySelector('#geofinder-button-open')?.click()")
        await page.wait_for_timeout(SHORT_WAIT_MS)
        await page.locator("#geofinder-input-level1").fill(REGION_OBJETIVO, timeout=ELEMENT_TIMEOUT)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(SHORT_WAIT_MS)
        await page.locator("#geofinder-input-level2").fill(COMUNA_SODIMAC, timeout=ELEMENT_TIMEOUT)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(SHORT_WAIT_MS)
    except Exception:
        pass


async def _extraer_precio_sodimac(page):
    precio = await page.evaluate(
        """() => {
            const candidatos = [
                "[id^='testId-pod-prices-'] li[data-normal-price]",
                ".pdp-prices li[data-normal-price]",
                ".prices-container li[data-normal-price]"
            ];
            for (const sel of candidatos) {
                const el = document.querySelector(sel);
                if (!el) continue;
                if (el.hasAttribute("data-normal-price")) {
                    return el.getAttribute("data-normal-price");
                }
                const txt = (el.textContent || "").trim();
                if (txt) return txt;
            }
            return null;
        }"""
    )
    if precio:
        return _formatear_precio(precio)

    precio_ld = await page.evaluate(
        """() => {
            const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
            for (const s of scripts) {
                try {
                    const data = JSON.parse(s.textContent || '{}');
                    if (data['@type'] === 'Product') {
                        const offers = data.offers || {};
                        const price = Array.isArray(offers) ? offers[0]?.price : offers.price;
                        if (price) return String(price);
                    }
                } catch (e) {}
            }
            return null;
        }"""
    )
    return _formatear_precio(precio_ld)


async def _extraer_precio_easy(page):
    precio = await page.evaluate(
        """() => {
            const nombre = document.querySelector("h1[data-id='product-name']");
            const bloque = nombre?.closest("div.sc-11b00991-0") || nombre?.parentElement;
            const raiz = bloque || document;
            const candidatos = [
                "[data-id='product-price']",
                "span.sc-11b00991-5 div.sc-1f784e80-3",
                "span.sc-11b00991-5",
                "div.sc-1f784e80-3",
            ];
            for (const sel of candidatos) {
                const el = raiz.querySelector(sel);
                if (!el) continue;
                const txt = (el.textContent || "").trim();
                if (txt.includes("$")) return txt;
            }
            return null;
        }"""
    )
    if precio:
        return _formatear_precio(precio)

    try:
        texto = await page.locator("text=/\\$ *\\d/").first.text_content(timeout=1500)
    except Exception:
        texto = None
    if not texto:
        return None
    match = re.search(r"\$ *\d[\d.,]*", texto)
    return _formatear_precio(match.group(0) if match else None)


async def _abrir_producto_identificado(page, producto, selector_css, usar_fallback=True):
    candidatos = await page.evaluate(
        """(selector) => {
            return [...document.querySelectorAll(selector)]
                .slice(0, 30)
                .map(a => ({
                    href: a.getAttribute('href') || '',
                    text: (a.textContent || '').trim()
                }));
        }""",
        selector_css,
    )

    candidatos = [c for c in candidatos if _href_util(c.get("href"))]

    href = _elegir_mejor_link(producto, candidatos)
    if not href and usar_fallback:
        for item in candidatos:
            posible = (item.get("href") or "").strip()
            if posible:
                href = posible
                break
    if not href:
        return False

    if not href.startswith(("http://", "https://", "/")):
        href = f"/{href.lstrip('./')}"
    destino = urljoin(page.url, href)
    if destino.endswith("/p/p"):
        destino = destino[:-2]
    await page.goto(destino, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_timeout(120)
    return True


async def _abrir_producto_castro(page, producto):
    dimension = _dimension_objetivo(producto)
    candidatos = await page.evaluate(
        """() => {
            return [...document.querySelectorAll(".product-title a, .thumbnail.product-thumbnail, .product-miniature a")]
                .slice(0, 40)
                .map(a => ({
                    href: a.getAttribute('href') || '',
                    text: (a.textContent || '').trim()
                }));
        }"""
    )
    candidatos = [c for c in candidatos if _href_util(c.get("href"))]
    if dimension:
        candidatos = [
            c for c in candidatos
            if _coincide_dimension(f"{c.get('text', '')} {c.get('href', '')}", dimension)
        ]
    if not candidatos:
        return False

    href = _elegir_mejor_link(producto, candidatos)
    if not href:
        return False
    if not href.startswith(("http://", "https://", "/")):
        href = f"/{href.lstrip('./')}"
    destino = urljoin(page.url, href)
    await page.goto(destino, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_timeout(120)

    try:
        titulo = await page.locator("h1[itemprop='name'], h1.h1, h1").first.text_content(timeout=6000)
    except Exception:
        titulo = ""
    if not _coincide_dimension(titulo or "", dimension) and not _coincide_dimension(page.url, dimension):
        return False
    return True


async def buscar_sodimac(browser, producto):
    context = await _crear_contexto(browser)
    page = await context.new_page()
    precio = None
    url = "https://www.sodimac.cl/sodimac-cl/"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await _limpiar_modales(page)
        await _configurar_ubicacion_sodimac(page)

        search_input = page.locator(
            '#testId-SearchBar-Input, input[placeholder*="Buscar en Sodimac"]'
        ).first
        await search_input.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
        await search_input.click(force=True)
        await search_input.fill(producto)
        await search_input.press("Enter")

        try:
            await page.wait_for_url("**/buscar**", timeout=RESULT_TIMEOUT)
        except Exception:
            pass
        try:
            await page.locator("a[href*='/articulo/']").first.wait_for(state="visible", timeout=RESULT_TIMEOUT)
        except Exception:
            pass
        await page.wait_for_timeout(SHORT_WAIT_MS)

        try:
            abierto = await _abrir_producto_identificado(
                page,
                producto,
                ".product-item a, [data-testid='product-card'] a, [data-testid='product-pod'] a",
            )
            if not abierto:
                abierto = await _abrir_producto_identificado(
                    page,
                    producto,
                    "a[href*='/articulo/']",
                )
            if abierto:
                try:
                    await page.locator("li[data-normal-price]").first.wait_for(
                        state="visible",
                        timeout=RESULT_TIMEOUT,
                    )
                except Exception:
                    pass
                precio = await _extraer_precio_sodimac(page)
            else:
                precio = None
        except Exception:
            precio = None
        url = page.url
    except Exception:
        url = page.url
    finally:
        await context.close()
    return precio, url


async def buscar_easy(browser, producto, on_status=None):
    context = await _crear_contexto(browser)
    page = await context.new_page()
    precio = None
    url = "https://www.easy.cl/"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await _limpiar_modales(page)
        await _configurar_ubicacion_easy(page)

        search_url = f"https://www.easy.cl/search/{quote(producto)}"
        candidatos_ok = False
        for intento in range(3):
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                await page.wait_for_timeout(SHORT_WAIT_MS + (intento * 250))
                candidatos = await page.evaluate(
                    """() => [...document.querySelectorAll(
                        ".product-card a, .product-item a, [data-testid='product-card'] a, [data-testid*='product'] a[href*='/p']"
                    )].slice(0, 40).map(a => ({ href: a.getAttribute('href') || '', text: (a.textContent || '').trim() }))"""
                )
                candidatos_ok = any(_href_util(c.get("href")) for c in candidatos)
                if candidatos_ok:
                    break
            except Exception:
                pass
            await page.wait_for_timeout(RETRY_WAIT_MS)
        if not candidatos_ok:
            if on_status:
                on_status("Reintentando Easy...")
            try:
                await page.goto("https://www.easy.cl/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                buscador = page.locator(
                    "input[type='search'], input[placeholder*='Buscar'], input[name='search']"
                ).first
                await buscador.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
                await buscador.click(force=True)
                await buscador.fill(producto)
                await buscador.press("Enter")
                await page.wait_for_timeout(700)
            except Exception:
                pass
        try:
            await page.locator("[data-testid*='product'] a[href$='/p']").first.wait_for(
                state="visible",
                timeout=RESULT_TIMEOUT,
            )
        except Exception:
            pass
        await page.wait_for_timeout(SHORT_WAIT_MS)

        try:
            abierto = await _abrir_producto_identificado(
                page,
                producto,
                ".product-card a, .product-item a, [data-testid='product-card'] a",
            )
            if not abierto:
                abierto = await _abrir_producto_identificado(
                    page,
                    producto,
                    "[data-testid*='product'] a[href*='/p']",
                )
            if abierto:
                precio = await _extraer_precio_easy(page)
            else:
                precio = None
        except Exception:
            precio = None
        url = page.url
    except Exception:
        url = page.url
    finally:
        await context.close()
    return precio, url


async def _seleccionar_talca_barraca(page):
    try:
        await page.goto("https://www.barracacastro.cl/bienvenida/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        boton_talca = page.locator("#talca").first
        await boton_talca.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
        await boton_talca.click(force=True)
        await page.wait_for_timeout(350)
    except Exception:
        pass


async def _extraer_precio_castro(page):
    precio = await page.evaluate(
        """() => {
            const selectors = [
                ".product-prices .current-price [itemprop='price']",
                ".product-prices [itemprop='price']",
                ".product-price .price",
                "span.price[itemprop='price']",
                ".current-price .price",
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (!el) continue;
                const porContenido = el.getAttribute("content");
                if (porContenido) return porContenido;
                const txt = (el.textContent || "").trim();
                if (txt) return txt;
            }
            return null;
        }"""
    )
    if precio:
        return _formatear_precio(precio)
    return None


async def buscar_castro(browser, producto):
    context = await _crear_contexto(browser)
    page = await context.new_page()
    precio = None
    search_url = f"https://www.barracacastro.cl/tiendaonline/busqueda?controller=search&s={quote(producto)}"
    url = search_url
    try:
        await _seleccionar_talca_barraca(page)
        await page.goto(search_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        try:
            await page.locator(
                ".thumbnail.product-thumbnail, .product-title a, .product-miniature a"
            ).first.wait_for(state="visible", timeout=RESULT_TIMEOUT + 2500)
        except Exception:
            pass

        abierto = await _abrir_producto_castro(page, producto)
        if abierto:
            precio = await _extraer_precio_castro(page)
            url = page.url
    except Exception:
        url = search_url
    finally:
        await context.close()
    return precio, url


def buscar_precios(producto, on_status=None):
    async def main():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, slow_mo=0, args=BROWSER_ARGS)
            try:
                sodimac, easy, castro = await asyncio.gather(
                    buscar_sodimac(browser, producto),
                    buscar_easy(browser, producto, on_status=on_status),
                    buscar_castro(browser, producto),
                )
                return sodimac, easy, castro
            except Exception as e:
                print(f"Error: {e}")
                return (None, None), (None, None), (None, None)
            finally:
                await browser.close()

    return asyncio.run(main())
