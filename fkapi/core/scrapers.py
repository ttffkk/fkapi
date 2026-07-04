# Standard library imports
import logging
import re
import time
from contextlib import contextmanager
from typing import Any

# Third-party imports
import requests
from bs4 import BeautifulSoup
from colorama import Fore

# Django imports
from django.db import connection, transaction

from core.constants import (
    BASE_URL,
    BRAND_SLUG_SUFFIX,
    COLLECTION_CONTAINER_CLASS,
    CURRENT_SEASON_YEAR,
    DEFAULT_LOGO_URL,
    HTML_PARSER,
    HTTP_STATUS_FORBIDDEN,
    KIT_CLASS,
    KIT_CONTAINER_CLASS,
    KIT_SEASON_CLASS,
    LATEST_PAGE_URL,
    MAX_RETRIES,
    MSG_403_RETRY_PROXY,
    RETRY_DELAY,
    SECTION_DETAILS_CLASS,
)
from core.exceptions import (
    ClubNotFoundError,
    InvalidSeasonError,
    KitNotFoundError,
    RateLimitExceededError,
    ScrapingError,
)
from core.http import HTTP_DEFAULT_HEADERS, http_get
from core.parsers import extract_fact_table, parse_kit_page
from core.services.scraping_service import ScrapingService

# Local imports
from .models import Brand, Club, Competition, Kit, Season, Type_K

logger = logging.getLogger(__name__)


def _split_slug_trailing_digits(slug: str) -> tuple[str, str] | None:
    """Split slug into (base, trailing_digits) if it ends with digits; else None. O(n), no regex."""
    if not slug or not slug[-1].isdigit():
        return None
    i = len(slug) - 1
    while i >= 0 and slug[i].isdigit():
        i -= 1
    if i < len(slug) - 1:
        return slug[: i + 1], slug[i + 1 :]
    return None


def build_kit_url(slug: str, kit_id: str | None = None) -> str:
    """
    Builds the complete URL for scraping a kit.

    Args:
        slug (str): The kit's base slug
        kit_id (str, optional): The kit's ID for new format URLs

    Returns:
        str: Complete URL for scraping
    """
    if kit_id:
        return f"{BASE_URL}/{slug}/{kit_id}/"
    split = _split_slug_trailing_digits(slug)
    if split:
        base_slug, extracted_id = split
        return f"{BASE_URL}/{base_slug}/{extracted_id}/"
    return f"{BASE_URL}/{slug}"


@contextmanager
def db_connection() -> Any:
    try:
        yield
    finally:
        connection.close()


def get_current_season() -> Season:
    """
    Gets the current season (2024).

    Returns:
        Season: Season object corresponding to the current season

    Raises:
        Season.DoesNotExist: If the 2024 season does not exist
    """
    try:
        return Season.objects.get(first_year=CURRENT_SEASON_YEAR)
    except Season.DoesNotExist as e:
        raise Season.DoesNotExist(f"The {CURRENT_SEASON_YEAR} season was not found") from e


def _normalize_season_slug(slug: str) -> str:
    s = slug.strip()
    while True:
        lower = s.lower()
        idx = lower.find("(carry-over)")
        if idx < 0:
            break
        s = (s[:idx] + s[idx + 12 :]).strip()
    return s


def _parse_two_digit_year(slug: str) -> tuple[str, str, str | None] | None:
    m = re.match(r"^(\d{2})(?:-(\d{2}))?$", slug)
    if not m or len(slug) > 7:
        return None
    first_short, second_short = m.group(1), m.group(2)
    first_century = "19" if int(first_short) >= 50 else "20"
    first_year = first_century + first_short
    if not second_short:
        return (first_year, first_year, None)
    first_year_int = int(first_year)
    second_candidate = int(first_century + second_short)
    if second_candidate < first_year_int:
        second_century = "20" if first_century == "19" else "21"
    else:
        second_century = first_century
    second_year = second_century + second_short
    return (f"{first_year}-{second_short}", first_year, second_year)


def _parse_direct_year(slug: str) -> tuple[str, str, str | None] | None:
    m = re.match(r"^(\d{4})(?:-(\d{2}|\d{4}))?$", slug)
    if not m:
        return None
    first_year = m.group(1)
    second_part = m.group(2)
    if not second_part:
        return (first_year, first_year, None)
    if len(second_part) == 2:
        fy_digit = int(first_year[2])
        sy_digit = int(second_part[0])
        century = str(int(first_year[:2]) + 1) if sy_digit < fy_digit else first_year[:2]
        second_year = century + second_part
        year_str = f"{first_year}-{second_part}"
    else:
        second_year = second_part
        year_str = f"{first_year}-{second_part}"
    return (year_str, first_year, second_year)


def _parse_year_from_kit_slug(slug: str) -> tuple[str, str, str | None]:
    all_matches = list(re.finditer(r"-(\d{4})(?:-(\d{2}))?-", slug + "-"))
    if not all_matches:
        raise ValueError(f"Could not find year in slug: {slug}")
    year_match = None
    for match in reversed(all_matches):
        if 1900 <= int(match.group(1)) <= 2100:
            year_match = match
            break
    year_match = year_match or all_matches[-1]
    first_year = year_match.group(1)
    second_short = year_match.group(2)
    if not second_short:
        return (first_year, first_year, None)
    fy_digit = int(first_year[2])
    sy_digit = int(second_short[0])
    century = str(int(first_year[:2]) + 1) if sy_digit < fy_digit else first_year[:2]
    second_year = century + second_short
    return (f"{first_year}-{second_short}", first_year, second_year)


def _validate_season_years(original_slug: str, first_year: str, second_year: str | None) -> None:
    try:
        first_year_int = int(first_year)
    except ValueError as err:
        raise InvalidSeasonError(original_slug, f"first_year '{first_year}' is not a valid integer") from err
    if first_year_int < 1800 or first_year_int > 2100:
        raise InvalidSeasonError(original_slug, f"first_year '{first_year}' is out of valid range (1800-2100)")
    if not second_year:
        return
    try:
        second_year_int = int(second_year)
    except ValueError as err:
        raise InvalidSeasonError(original_slug, f"second_year '{second_year}' is not a valid integer") from err
    if second_year_int < 1800 or second_year_int > 2100:
        raise InvalidSeasonError(original_slug, f"second_year '{second_year}' is out of valid range (1800-2100)")
    if second_year_int < first_year_int:
        raise InvalidSeasonError(
            original_slug,
            f"second_year '{second_year}' ({second_year_int}) cannot be before first_year '{first_year}' ({first_year_int})",
        )
    year_span = second_year_int - first_year_int
    max_span = 20 if first_year_int < 1960 else 2
    if year_span > max_span:
        raise InvalidSeasonError(
            original_slug,
            f"Year span is {year_span} years (max allowed: {max_span}). first_year: {first_year}, second_year: {second_year}",
        )


def _get_or_create_season(year_str: str, first_year: str, second_year: str | None) -> Season:
    try:
        return Season.objects.get(year=year_str)
    except Season.DoesNotExist:
        return Season.objects.create(year=year_str, first_year=first_year, second_year=second_year)


def get_season(season_slug: str) -> Season:
    """
    Retrieves or creates a Season object using the full year from the slug.

    Args:
        season_slug: The full URL slug (e.g., 'olympique-marseille-2020-21-third-kit')
                     or a direct year string (e.g., '2024', '2023-24', '1999-00').

    The function handles both full kit slugs and direct year strings.
    """
    try:
        slug = _normalize_season_slug(season_slug)
        parsed = _parse_two_digit_year(slug)
        if parsed:
            year_str, first_year, second_year = parsed
            return _get_or_create_season(year_str, first_year, second_year)
        parsed = _parse_direct_year(slug)
        if parsed is None:
            parsed = _parse_year_from_kit_slug(slug)
        year_str, first_year, second_year = parsed
        _validate_season_years(season_slug, first_year, second_year)
        return _get_or_create_season(year_str, first_year, second_year)
    except Exception as e:
        raise InvalidSeasonError(season_slug, f"Error processing season from slug '{season_slug}': {str(e)}") from e


def get_season_e(season_slug: str) -> Season:
    """
    Extended version of get_season that handles additional formats like '2023-2024'.
    Normalizes formats before passing to get_season.
    """
    import re

    # Check if it's in the format '2023-2024' (4-digit year - 4-digit year)
    match = re.match(r"^(\d{4})-(\d{4})$", season_slug)
    if match:
        first_year = match.group(1)
        second_year = match.group(2)

        # Create or get the season
        try:
            return Season.objects.get(year=season_slug)
        except Season.DoesNotExist:
            return Season.objects.create(year=season_slug, first_year=first_year, second_year=second_year)

    # For other formats, use the standard get_season function
    return get_season(season_slug)


def _scrape_kit_raise_if_page_not_found(soup: BeautifulSoup, slug: str) -> None:
    page_not_found_text = soup.find(
        string=lambda text: text and "The requested page could not be found" in text
    )
    if page_not_found_text:
        logger.warning(f"Page moved: Kit {slug} - 'The requested page could not be found'")
        raise KitNotFoundError(slug, f"Kit not found: {slug}")
    if soup.find("h1", string="404 Not Found"):
        logger.warning(f"404 Not Found: Kit {slug} does not exist")
        raise KitNotFoundError(slug, f"Kit not found: {slug}")


def _scrape_kit_handle_404(
    soup: BeautifulSoup, slug: str, kit_id: str | None, retry_count: int, max_retries: int
) -> tuple[str | None, str | None, int]:
    try:
        extract_fact_table(soup)
        logger.warning(f"Got 404 status but found fact table for {slug}. Likely network issue, retrying...")
        next_count = retry_count + 1
        if next_count >= max_retries:
            logger.error(f"Max retries reached for {slug} with 404 status")
            return None, None, next_count
        return slug, kit_id, next_count
    except ValueError:
        pass
    logger.warning(f"404 status and no fact table found for {slug}. Trying new URL format...")
    base_slug, extracted_kit_id = _try_new_url_format(slug)
    if base_slug and extracted_kit_id:
        next_count = retry_count + 1
        if next_count >= max_retries:
            logger.error(f"Max retries reached for {slug}")
            return None, None, next_count
        return base_slug, extracted_kit_id, next_count
    logger.warning(f"No new URL format available for {slug}. Page likely moved.")
    raise KitNotFoundError(slug, f"Kit not found: {slug}") from None


def _scrape_kit_attempt(
    slug: str,
    kit_id: str | None,
    use_proxy: bool,
    existing_kit_id: int | None,
    retry_count: int,
    max_retries: int,
) -> tuple[str, Kit | None, str | None, str | None, bool, int]:
    logger.debug(f"Attempting to scrape kit: {slug} (attempt {retry_count + 1}/{max_retries})")
    logger.debug(f"Using proxy: {use_proxy}")
    url = build_kit_url(slug, kit_id)
    logger.debug(f"Making request to {url}")
    response = http_get(url, use_proxy=use_proxy)
    logger.debug(f"Response status code: {response.status_code}")
    if response.status_code == HTTP_STATUS_FORBIDDEN:
        if retry_count == max_retries - 1:
            raise RateLimitExceededError("Rate limit exceeded while scraping kit")
        logger.warning(MSG_403_RETRY_PROXY)
        return ("retry_403", None, slug, kit_id, True, retry_count + 1)
    if response.status_code == 404:
        logger.warning(f"Received 404 status code for {slug}. This could be network issue or page moved.")
    else:
        response.raise_for_status()
    logger.debug("Parsing HTML response")
    soup = BeautifulSoup(response.text, HTML_PARSER)
    _scrape_kit_raise_if_page_not_found(soup, slug)
    if response.status_code == 404:
        next_slug, next_kit_id, next_count = _scrape_kit_handle_404(
            soup, slug, kit_id, retry_count, max_retries
        )
        if next_slug is None:
            return ("done", None, None, None, False, next_count)
        logger.debug(f"Retrying with new URL format: {slug}/{kit_id}/" if next_kit_id else f"Retrying: {next_slug}")
        return ("retry_404", None, next_slug, next_kit_id, False, next_count)
    data = parse_kit_page(soup)
    kit = ScrapingService.process_kit_data(data, slug, kit_id, use_proxy, existing_kit_id)
    return ("ok", kit, None, None, False, retry_count)


def scrape_kit(
    slug: str, kit_id: str | None = None, use_proxy: bool = False, existing_kit_id: int | None = None
) -> Kit | None:
    """
    Scrapes a kit from footballkitarchive.com using its slug and optional kit_id.

    Args:
        slug (str): The kit's base slug from the URL
        kit_id (str, optional): The kit's ID for new format URLs. Defaults to None.
        use_proxy (bool, optional): Whether to use a proxy for the request. Defaults to False.
        existing_kit_id (int, optional): ID of existing kit to update. Defaults to None.

    Returns:
        Optional[Kit]: The created/updated Kit object, or None if scraping failed

    Raises:
        requests.exceptions.RequestException: For network-related errors
        ValueError: For invalid data in the response
    """
    max_retries = MAX_RETRIES
    retry_count = 0

    while retry_count < max_retries:
        try:
            outcome, kit, next_slug, next_kit_id, use_proxy_next, retry_count = _scrape_kit_attempt(
                slug, kit_id, use_proxy, existing_kit_id, retry_count, max_retries
            )
            if outcome in ("ok", "done"):
                return kit
            slug = next_slug or slug
            kit_id = next_kit_id or kit_id
            use_proxy = use_proxy_next
            time.sleep(RETRY_DELAY)
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error scraping {slug}: {str(e)}")
            retry_count += 1
            if retry_count == max_retries:
                logger.error(f"Max retries ({max_retries}) reached. Giving up.")
                return None
            logger.warning(f"Retrying in 2 seconds... (attempt {retry_count + 1}/{max_retries})")
            time.sleep(2)
        except KitNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error scraping {slug}: {str(e)}")
            return None


def _try_new_url_format(slug: str) -> tuple[str | None, str | None]:
    """
    Tries to convert old URL format to new format with ID.

    Old format: granada-cf-2025-26-third-kit402174
    New format: (base_slug, kit_id) = (granada-cf-2025-26-third-kit, 402174)

    Args:
        slug (str): The old slug format

    Returns:
        tuple[Optional[str], Optional[str]]: (base_slug, kit_id) if conversion is possible, (None, None) otherwise
    """
    split = _split_slug_trailing_digits(slug)
    if split:
        base_slug, kit_id = split
        print(Fore.CYAN + f"[DEBUG] Converting {slug} -> base: {base_slug}, id: {kit_id}")
        return base_slug, kit_id
    return None, None


def scrape_competition(slug: str, use_proxy: bool = False) -> Competition | None:
    """
    Scrapes a competition from footballkitarchive.com using its slug.

    Args:
        slug (str): The competition's slug from the URL
        use_proxy (bool, optional): Whether to use a proxy for the request. Defaults to False.

    Returns:
        Optional[Competition]: The created/updated Competition object, or None if scraping failed

    Raises:
        requests.exceptions.RequestException: For network-related errors
        ValueError: For invalid data in the response
    """
    max_retries = MAX_RETRIES
    retry_count = 0

    while retry_count < max_retries:
        try:
            response = http_get(f"{BASE_URL}/{slug}", use_proxy=use_proxy)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, HTML_PARSER)

            # Get competition name
            title = soup.find("span", class_="main-title")
            if not title:
                raise ValueError("Competition title not found")

            name = title.text.strip()

            # Create or update competition
            competition, created = Competition.objects.update_or_create(
                slug=slug.replace("/", ""), defaults={"name": name}
            )

            if created:
                print(Fore.GREEN + f"Created new competition: {competition}")
            else:
                print(Fore.CYAN + f"Updated competition: {competition}")

            return competition

        except requests.exceptions.RequestException as e:
            print(Fore.RED + f"Network error scraping competition {slug}: {str(e)}")
            retry_count += 1
            if retry_count == max_retries:
                return None
            time.sleep(2)

        except Exception as e:
            print(Fore.RED + f"Error scraping competition {slug}: {str(e)}")
            return None


def _parse_main_header_logo(main_header: BeautifulSoup) -> tuple[str, str | None]:
    logo_img = main_header.find("img")
    if not logo_img or "data-src" not in logo_img.attrs:
        raise ValueError("Logo image not found")
    logo_path = logo_img["data-src"].lstrip("/")
    logo_url = f"{BASE_URL.rstrip('/')}/{logo_path}"
    if "_l" in logo_url:
        return logo_url.replace("_l", ""), logo_url
    return logo_url, None


def _scrape_club_apply_logo(club: Club, main_header: BeautifulSoup) -> None:
    try:
        logo_url, logo_dark = _parse_main_header_logo(main_header)
        club.logo = logo_url
        if logo_dark is not None:
            club.logo_dark = logo_dark
    except Exception as e:
        print(Fore.YELLOW + f"Logo error for {club}: {str(e)}. Using default.")
        club.logo = DEFAULT_LOGO_URL


def _scrape_club_details_fetch(slug: str, use_proxy: bool) -> Club:
    response = http_get(f"{BASE_URL}/{slug}", use_proxy=use_proxy)
    if response.status_code == 403:
        raise RateLimitExceededError("Rate limit exceeded while scraping club")
    if response.status_code == 404:
        raise ClubNotFoundError(slug, f"Club not found: {slug}")
    response.raise_for_status()
    soup = BeautifulSoup(response.text, HTML_PARSER)
    title_elem = soup.find("span", class_="main-title")
    if not title_elem:
        raise ValueError("Club title not found")
    name = title_elem.text.replace("Kit History", "").strip()
    main_header = soup.find("div", class_="main-header")
    if not main_header:
        raise ValueError("Club header not found")
    with transaction.atomic():
        club, created = Club.objects.update_or_create(slug=slug, defaults={"name": name})
        _scrape_club_apply_logo(club, main_header)
        club.save()
    status = "Created" if created else "Updated"
    print(Fore.GREEN + f"{status} club: {club}")
    return club


def scrape_club_details(slug: str, use_proxy: bool = False) -> Club | None:
    """
    Scrapes club details from footballkitarchive.com using its slug.

    Args:
        slug (str): The club's slug from the URL
        use_proxy (bool, optional): Whether to use a proxy for the request. Defaults to False.

    Returns:
        Optional[Club]: The created/updated Club object, or None if scraping failed

    Raises:
        requests.exceptions.RequestException: For network-related errors
        ValueError: For invalid data in the response
    """
    max_retries = MAX_RETRIES
    retry_count = 0
    while retry_count < max_retries:
        print(Fore.MAGENTA + f"Scraping {slug}" + (" with proxy" if use_proxy else ""))
        try:
            return _scrape_club_details_fetch(slug, use_proxy)
        except requests.exceptions.RequestException as e:
            print(Fore.RED + f"Network error scraping club {slug}: {str(e)}")
            retry_count += 1
            if retry_count == max_retries:
                return None
            time.sleep(2)
        except Exception as e:
            print(Fore.RED + f"Error scraping club {slug}: {str(e)}")
            return None
    return None


def _scrape_whole_club_process_season(season_container: BeautifulSoup, club: Club) -> None:
    season_header = season_container.find("h3")
    if not season_header:
        print(Fore.YELLOW + "Season header not found, skipping...")
        return
    season_year = season_header.text.strip()
    print(Fore.YELLOW + f"Processing season {season_year}")
    try:
        season = get_season_e(season_year)
    except Exception as e:
        print(Fore.RED + f"Error getting season {season_year}: {str(e)}")
        return
    details = season_container.find("ul", class_=SECTION_DETAILS_CLASS)
    if not details or not details.find_all("a"):
        print(Fore.YELLOW + f"No brand found for season {season_year}")
        return
    brand_link = details.find_all("a")[0]
    brand_slug = brand_link["href"].replace("/", "")
    brand = _get_or_create_brand(brand_slug)
    if not brand:
        print(Fore.YELLOW + f"Could not get/create brand {brand_slug}, skipping season...")
        return
    kits_lite = season_container.find_all("div", class_=KIT_CLASS)
    if not kits_lite:
        print(Fore.YELLOW + f"No kits found for season {season_year}")
        return
    for kit_lite in kits_lite:
        try:
            kit = _process_kit(kit_lite, club, season, brand)
            if kit:
                print(Fore.GREEN + f"Successfully processed kit: {kit.name}")
        except Exception as e:
            print(Fore.RED + f"Error processing kit: {str(e)}")
    print(Fore.CYAN + f"Completed season {season_year} for {club.name}")


def _extract_kit_slugs(container: BeautifulSoup) -> list[str]:
    """Harvest kit slugs from a club page's anchors.

    footballkitarchive.com no longer puts the kit link inside each kit tile
    (those are now lazy-loaded image placeholders). The kits are plain anchors
    whose last path segment ends in ``-<digits>`` — the numeric kit id, e.g.
    ``/arsenal-fc-2026-27-home-kit-441205/``. Club/brand/competition/season
    links instead end in ``-<letter><digits>`` (``-t9``, ``-b28``, ``-l224``),
    so a purely numeric trailing segment cleanly identifies a kit. We return the
    slug with the id stripped (``arsenal-fc-2026-27-home-kit``), which the kit
    page resolves on its own, de-duplicated in document order.
    """
    slugs: list[str] = []
    seen: set[str] = set()
    for anchor in container.find_all("a"):
        href = anchor.get("href") or ""
        segment = href.strip("/").split("/")[-1]
        base, sep, tail = segment.rpartition("-")
        if sep and base and tail.isdigit() and base not in seen:
            seen.add(base)
            slugs.append(base)
    return slugs


def _scrape_whole_club_fetch(club: Club) -> Club:
    response = http_get(f"{BASE_URL}/{club.slug}", use_proxy=True)
    if response.status_code == 403:
        raise RateLimitExceededError("Rate limit exceeded while scraping club")
    if response.status_code == 404:
        raise ClubNotFoundError(club.slug, f"Club not found: {club.slug}")
    response.raise_for_status()
    soup = BeautifulSoup(response.text, HTML_PARSER)
    container = soup.find("div", class_="archive-content-container")
    if not container:
        raise ValueError("Archive content container not found")
    kit_slugs = _extract_kit_slugs(container)
    if not kit_slugs:
        print(Fore.YELLOW + f"No kit links found for {club.name}")
        return club
    print(Fore.CYAN + f"Found {len(kit_slugs)} kits for {club.name}; scraping each page...")
    saved = 0
    # No outer transaction: each kit is saved independently by scrape_kit, so
    # one bad kit can't roll back the rest.
    for slug in kit_slugs:
        try:
            result = scrape_kit(slug, use_proxy=True)
            if isinstance(result, Kit):
                saved += 1
                print(Fore.GREEN + f"Saved kit: {slug}")
            else:
                print(Fore.YELLOW + f"Skipped kit (no data): {slug}")
        except Exception as e:  # noqa: BLE001 - one kit failing must not abort the club
            print(Fore.RED + f"Error scraping kit {slug}: {str(e)}")
    print(Fore.GREEN + f"Scraped {saved}/{len(kit_slugs)} kits for {club.name}")
    return club


def scrape_whole_club(club: Club) -> Club | None:
    """
    Scrapes all kits for a given club from footballkitarchive.com.
    Always uses a proxy to prevent IP bans when making multiple requests.

    Args:
        club (Club): The club to scrape kits for

    Returns:
        Optional[Club]: The club object if successful, None if failed

    Raises:
        requests.exceptions.RequestException: For network-related errors
        ValueError: For invalid data in the response
    """
    max_retries = MAX_RETRIES
    retry_count = 0
    while retry_count < max_retries:
        print(Fore.MAGENTA + f"Scraping all kits for {club.name} with proxy")
        try:
            return _scrape_whole_club_fetch(club)
        except requests.exceptions.RequestException as e:
            print(Fore.RED + f"Network error scraping {club.name}: {str(e)}")
            retry_count += 1
            if retry_count == max_retries:
                return None
            time.sleep(2)
        except Exception as e:
            print(Fore.RED + f"Error scraping {club.name}: {str(e)}")
            return None
    return None


def _get_or_create_brand(brand_slug: str) -> Brand | None:
    """Helper function to get or create a brand."""
    try:
        return Brand.objects.get(slug=brand_slug)
    except Brand.DoesNotExist:
        try:
            return scrape_brand(brand_slug, True)
        except Exception as e:
            # Format brand name from slug by removing -kits suffix and formatting
            brand_name = brand_slug
            if brand_name.endswith(BRAND_SLUG_SUFFIX):
                brand_name = brand_name[: -len(BRAND_SLUG_SUFFIX)]  # Remove -kits suffix
            brand_name = brand_name.replace("-", " ").strip().title()

            print(f"Logo error for {brand_name}: {str(e)}. Using default.")
            brand = Brand.objects.create(name=brand_name, slug=brand_slug, logo=DEFAULT_LOGO_URL)
            print(f"Created brand: {brand.name}")
            return brand


def _process_kit(kit_element: BeautifulSoup, club: Club, season: Season, brand: Brand) -> Kit | None:
    """Helper function to process a single kit element."""
    try:
        # Extract kit data
        kit_link = kit_element.find("a")
        if not kit_link:
            return None

        # Extract slug and kit_id from href (format: slug/id/ or slug/id)
        href = kit_link["href"]
        slug_parts = href.strip("/").split("/")
        clean_slug = slug_parts[0]
        kit_id = slug_parts[1] if len(slug_parts) > 1 else None

        # Delete existing kit if it exists (for re-scraping)
        existing_kit = Kit.objects.filter(slug=clean_slug).first()
        if existing_kit:
            print(Fore.YELLOW + f"Deleting existing kit {clean_slug} for re-scrape...")
            existing_kit.delete()

        # Get kit type
        type_text = kit_element.find("div", class_=KIT_SEASON_CLASS)
        if not type_text:
            return None
        print(Fore.CYAN + f"[DEBUG] type_text: {type_text.text}")
        if season.year in type_text.text:
            type_name = type_text.text.strip().replace(season.year, "").strip()
        else:
            unformatted_year = f"{season.first_year}-{season.second_year[:2]}"
            type_name = type_text.text.strip().replace(unformatted_year, "").strip()
        type_k, created = Type_K.objects.get_or_create(name=type_name)
        if created:
            type_k.categorize()
            type_k.save()

        # Scrape full kit details
        return scrape_kit_lite(clean_slug, brand, season, type_k, club, kit_id=kit_id)

    except Exception as e:
        print(Fore.YELLOW + f"Error processing kit: {str(e)}")
        return None


def _scrape_kit_lite_parse_table(
    soup: BeautifulSoup, season: Season, type_k: Type_K
) -> dict[str, Any]:
    table = soup.find("table", class_="fact-table")
    if not table:
        raise ValueError("Kit fact table not found")
    table_rows = table.find_all("tr")
    kit_name_cell = table_rows[0].find_all("td")[1] if table_rows else None
    if not kit_name_cell:
        raise ValueError("Kit name not found")
    kit_name = f"{kit_name_cell.text.strip()} {season} {type_k}"
    design = None
    for row in table_rows:
        header = row.find("td")
        if header and header.text.strip() == "Design":
            design = row.find_all("td")[1].text.strip()
            break
    colors_data = None
    for row in table_rows:
        header = row.find("td")
        if header and header.text.strip() == "Colors":
            colors_data = row.find_all("td")[1].text.strip()
            break
    rating_span = soup.find("span", id="rating")
    rating_details = soup.find("span", id="rating-details-no")
    rating = (
        float(rating_span.text.strip())
        if (rating_span and rating_span.text.strip() and not rating_details)
        else 0.0
    )
    main_img = soup.find("img", class_="top-image")
    if not main_img or "data-src" not in main_img.attrs:
        raise ValueError("Main image not found")
    return {
        "kit_name": kit_name,
        "design": design,
        "colors_data": colors_data,
        "rating": rating,
        "main_img_url": main_img["data-src"],
        "table_rows": table_rows,
    }


def _scrape_kit_lite_save_and_enrich(
    slug: str,
    kit_id: str | None,
    club: Club,
    season: Season,
    type_k: Type_K,
    brand: Brand,
    parsed: dict[str, Any],
) -> Kit:
    from core.services.kits_service import KitsService

    kit_name = parsed["kit_name"]
    design = parsed["design"]
    colors_data = parsed["colors_data"]
    rating = parsed["rating"]
    main_img_url = parsed["main_img_url"]
    table_rows = parsed["table_rows"]
    kit, created = Kit.objects.get_or_create(
        slug=slug,
        defaults={
            "name": kit_name,
            "team": club,
            "season": season,
            "type": type_k,
            "brand": brand,
            "rating": rating,
            "kit_id": kit_id,
            "main_img_url": main_img_url,
            "design": design,
        },
    )
    if not created:
        kit.name = kit_name
        kit.team = club
        kit.season = season
        kit.type = type_k
        kit.brand = brand
        kit.rating = rating
        kit.main_img_url = main_img_url
        kit.design = design
        if kit_id:
            kit.kit_id = kit_id
        kit.save()
    competitions_cell = None
    for row in table_rows:
        header = row.find("td")
        if header and header.text.strip() == "League":
            competitions_cell = row.find_all("td")[1]
            break
    if competitions_cell:
        kit.competition.clear()
        KitsService.process_competitions(kit, str(competitions_cell), slug)
    if colors_data:
        kit.secondary_color.clear()
        kit.primary_color = None
        KitsService.process_colors(kit, colors_data)
    if created:
        print(Fore.GREEN + f"Created new kit: {kit}")
    else:
        print(Fore.CYAN + f"Updated existing kit: {kit}")
    return kit


def scrape_kit_lite(
    slug: str,
    brand: Brand,
    season: Season,
    type_k: Type_K,
    club: Club,
    kit_id: str | None = None,
    use_proxy: bool = True,
) -> Kit | None:
    """
    Scrapes a kit with minimal information, used for bulk scraping.

    Args:
        slug (str): The kit's slug from the URL
        brand (Brand): The kit's brand
        season (Season): The kit's season
        type_k (Type_K): The kit's type
        club (Club): The kit's club
        use_proxy (bool, optional): Whether to use a proxy for requests. Defaults to True.

    Returns:
        Optional[Kit]: The created Kit object, or None if scraping failed

    Raises:
        requests.exceptions.RequestException: For network-related errors
        ValueError: For invalid data in the response
    """
    max_retries = MAX_RETRIES
    retry_count = 0

    while retry_count < max_retries:
        try:
            # Make request
            response = http_get(build_kit_url(slug, kit_id), use_proxy=use_proxy)

            # Handle HTTP errors
            if response.status_code == HTTP_STATUS_FORBIDDEN:
                print(Fore.RED + "Received 403 status code. Retrying with a new proxy.")
                use_proxy = True
                retry_count += 1
                time.sleep(RETRY_DELAY)
                continue
            response.raise_for_status()

            # Parse HTML
            soup = BeautifulSoup(response.text, HTML_PARSER)

            parsed = _scrape_kit_lite_parse_table(soup, season, type_k)
            with transaction.atomic():
                return _scrape_kit_lite_save_and_enrich(
                    slug, kit_id, club, season, type_k, brand, parsed
                )

        except requests.exceptions.RequestException as e:
            print(Fore.RED + f"Network error scraping kit {slug}: {str(e)}")
            retry_count += 1
            if retry_count == max_retries:
                return None
            time.sleep(2)

        except Exception as e:
            print(Fore.RED + f"Error scraping kit {slug}: {str(e)}")
            return None


def _scrape_brand_apply_logo(brand: Brand, main_header: BeautifulSoup) -> None:
    try:
        logo_url, logo_dark = _parse_main_header_logo(main_header)
        brand.logo = logo_url
        if logo_dark is not None:
            brand.logo_dark = logo_dark
    except Exception as e:
        print(Fore.YELLOW + f"Logo error for {brand}: {str(e)}. Using default.")
        brand.logo = f"{BASE_URL}/static/logos/not_found.png"


def _scrape_brand_fetch(slug: str, use_proxy: bool) -> Brand:
    response = http_get(f"{BASE_URL}/{slug}", use_proxy=use_proxy)
    if response.status_code == 403:
        raise RateLimitExceededError("Rate limit exceeded while scraping brand")
    if response.status_code == 404:
        raise ScrapingError(f"Brand not found: {slug}", slug)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, HTML_PARSER)
    title_elem = soup.find("span", class_="main-title")
    if not title_elem:
        raise ValueError("Brand title not found")
    name = title_elem.text.replace("Football Kit History", "").strip()
    main_header = soup.find("div", class_="main-header")
    if not main_header:
        raise ValueError("Brand header not found")
    with transaction.atomic():
        brand, created = Brand.objects.get_or_create(slug=slug, defaults={"name": name})
        _scrape_brand_apply_logo(brand, main_header)
        brand.save()
    status = "Created" if created else "Updated"
    print(Fore.GREEN + f"{status} brand: {brand}")
    return brand


def scrape_brand(slug: str, use_proxy: bool = False) -> Brand | None:
    """
    Scrapes brand information from footballkitarchive.com using its slug.

    Args:
        slug (str): The brand's slug from the URL
        use_proxy (bool, optional): Whether to use a proxy for the request. Defaults to False.

    Returns:
        Optional[Brand]: The created/updated Brand object, or None if scraping failed

    Raises:
        requests.exceptions.RequestException: For network-related errors
        ValueError: For invalid data in the response
    """
    max_retries = MAX_RETRIES
    retry_count = 0
    while retry_count < max_retries:
        try:
            return _scrape_brand_fetch(slug, use_proxy)
        except requests.exceptions.RequestException as e:
            print(Fore.RED + f"Network error scraping brand {slug}: {str(e)}")
            retry_count += 1
            if retry_count == max_retries:
                return None
            time.sleep(2)
        except Exception as e:
            print(Fore.RED + f"Error scraping brand {slug}: {str(e)}")
            return None
    return None


def _scrape_latest_update_existing_kit_id(clean_slug: str, kit_id: str | None, existing_kit: Kit) -> None:
    if not kit_id:
        return
    try:
        full_kit = Kit.objects.get(id=existing_kit.id)
        if not full_kit.kit_id:
            print(Fore.YELLOW + f"Updating kit_id for existing kit: {clean_slug} -> {kit_id}")
            full_kit.kit_id = kit_id
            full_kit.save()
    except Exception:
        pass


def _scrape_latest_collect_one_kit(
    kit: Any,
) -> tuple[int, tuple[str, str | None] | None]:
    kit_link = kit.find("a")
    if not kit_link or "href" not in kit_link.attrs:
        return 0, None
    slug_parts = kit_link["href"].strip("/").split("/")
    clean_slug = slug_parts[0]
    kit_id = slug_parts[1] if len(slug_parts) > 1 else None
    try:
        existing_kit = Kit.objects.filter(slug=clean_slug).only("id", "slug").first()
        if existing_kit:
            _scrape_latest_update_existing_kit_id(clean_slug, kit_id, existing_kit)
            return 1, None
        return 0, (clean_slug, kit_id)
    except Exception as e:
        logger.error(f"Error checking kit {clean_slug}: {str(e)}")
        return 0, None


def _scrape_latest_collect_kits(
    kits: list,
) -> tuple[int, list[tuple[str, str | None]]]:
    existing_kits_count = 0
    new_kits: list[tuple[str, str | None]] = []
    for kit in kits:
        existing_delta, new_kit = _scrape_latest_collect_one_kit(kit)
        existing_kits_count += existing_delta
        if new_kit:
            new_kits.append(new_kit)
    return existing_kits_count, new_kits


def _scrape_lastest_page(page: int, use_proxy: bool) -> tuple[bool, bool]:
    from core.tasks import scrape_kit_task
    from core.utils.celery_utils import execute_task_async

    response = http_get(f"{BASE_URL}{LATEST_PAGE_URL}{page}", use_proxy=use_proxy)
    if response.status_code == HTTP_STATUS_FORBIDDEN:
        print(Fore.RED + "Received 403 status code. Retrying with a new proxy.")
        raise RateLimitExceededError("403 on latest page")
    response.raise_for_status()
    soup = BeautifulSoup(response.text, HTML_PARSER)
    kit_container = soup.find("div", class_=KIT_CONTAINER_CLASS)
    if not kit_container:
        raise ValueError("Kit container not found")
    kits = kit_container.find_all("div", class_=KIT_CLASS)
    if not kits:
        print(Fore.YELLOW + f"No kits found on page {page}")
        return True, True
    existing_kits_count, new_kits = _scrape_latest_collect_kits(kits)
    if existing_kits_count == len(kits):
        print(Fore.CYAN + f"All kits on page {page} already exist in database")
        return True, True
    if new_kits:
        print(Fore.YELLOW + f"Found {len(new_kits)} new kits on page {page}, processing in parallel...")
        for clean_slug, kit_id in new_kits:
            try:
                execute_task_async(scrape_kit_task, clean_slug, kit_id=kit_id, use_proxy=use_proxy)
            except Exception as e:
                logger.error(f"Error queuing kit {clean_slug} for processing: {str(e)}")
    print(Fore.GREEN + f"Successfully queued {len(new_kits)} new kits from page {page}")
    return True, False


def scrape_lastest(page: int = 1, use_proxy: bool = False) -> tuple[bool, bool]:
    """
    Scrapes the latest kits page from footballkitarchive.com.

    Args:
        page (int, optional): Page number to scrape. Defaults to 1.
        use_proxy (bool, optional): Whether to use a proxy for requests. Defaults to False.

    Returns:
        tuple[bool, bool]: (success, all_kits_exist)
            - success: True if page was successfully scraped, False otherwise
            - all_kits_exist: True if all kits on the page already exist in the database

    Raises:
        requests.exceptions.RequestException: For network-related errors
        ValueError: For invalid data in the response
    """
    max_retries = MAX_RETRIES
    retry_count = 0
    while retry_count < max_retries:
        try:
            return _scrape_lastest_page(page, use_proxy)
        except RateLimitExceededError:
            use_proxy = True
            retry_count += 1
            if retry_count >= max_retries:
                print(Fore.RED + f"Failed to scrape page {page} after {max_retries} attempts")
                return False, False
            time.sleep(RETRY_DELAY)
        except requests.exceptions.RequestException as e:
            print(Fore.RED + f"Network error on page {page}, attempt {retry_count + 1}: {str(e)}")
            retry_count += 1
            if retry_count == max_retries:
                print(Fore.RED + f"Failed to scrape page {page} after {max_retries} attempts")
                return False, False
            time.sleep(2)
        except Exception as e:
            print(Fore.RED + f"Unexpected error on page {page}: {str(e)}")
            return False, False
    return False, False


def _scrape_latest_pages_process_page(
    page: int, page_end: int, use_proxy: bool
) -> tuple[int, int, bool]:
    print(Fore.CYAN + f"\nProcessing page {page} of {page_end}")
    success, all_kits_exist = scrape_lastest(page, use_proxy)
    if success:
        if all_kits_exist:
            print(Fore.GREEN + f"Successfully scraped page {page} (all kits already exist)")
            print(Fore.CYAN + "Stopping scrape as all kits on this page already exist in database")
            return 1, 0, True
        print(Fore.GREEN + f"Successfully scraped page {page}")
        return 1, 0, False
    print(Fore.RED + f"Failed to scrape page {page}")
    return 0, 1, False


def _scrape_latest_pages_should_delay(
    page: int, page_end: int, reverse_order: bool
) -> bool:
    return (reverse_order and page > page_end) or (
        not reverse_order and page < page_end
    )


def scrape_latest_pages(
    page_start: int = 1,
    page_end: int = 1,
    use_proxy: bool = False,
    delay: int = 2,
    progress_callback: callable = None,
    reverse_order: bool = False,
) -> tuple[int, int]:
    """
    Scrapes a range of latest kit pages from footballkitarchive.com.

    Args:
        page_start (int, optional): First page to scrape. Defaults to 1.
        page_end (int, optional): Last page to scrape. Defaults to 1.
        use_proxy (bool, optional): Whether to use a proxy for requests. Defaults to False.
        delay (int, optional): Delay in seconds between pages. Defaults to 2.
        progress_callback (callable, optional): Callback function to report progress. Defaults to None.
        reverse_order (bool, optional): If True, process pages in reverse order. Defaults to False.

    Returns:
        tuple[int, int]: Count of (successful pages, failed pages)
    """
    if page_start > page_end and not reverse_order:
        raise ValueError("page_start cannot be greater than page_end unless reverse_order=True")

    success_count = 0
    failure_count = 0
    if reverse_order:
        page_range = range(page_start, page_end - 1, -1)
        print(Fore.CYAN + f"Starting backward scrape of pages {page_start} down to {page_end}")
    else:
        page_range = range(page_start, page_end + 1)
        print(Fore.CYAN + f"Starting forward scrape of pages {page_start} to {page_end}")

    for page in page_range:
        if progress_callback:
            progress_callback(page, page_start, page_end)
        try:
            succ, fail, stop = _scrape_latest_pages_process_page(
                page, page_end, use_proxy
            )
            success_count += succ
            failure_count += fail
            if stop:
                break
            if _scrape_latest_pages_should_delay(page, page_end, reverse_order):
                print(Fore.CYAN + f"Waiting {delay} seconds before next page...")
                time.sleep(delay)
        except Exception as e:
            failure_count += 1
            print(Fore.RED + f"Error processing page {page}: {str(e)}")

    print(Fore.CYAN + "\nScraping completed!")
    print(f"Pages processed successfully: {success_count}")
    print(f"Pages failed: {failure_count}")
    return success_count, failure_count


def scrape_user_info_api(userid: int) -> dict | None:
    """
    Scrape user information using the user.php API.

    Args:
        userid: User ID from FootballKitArchive

    Returns:
        dict | None: User information dict or None if request fails.
                     Structure: {"id": int, "name": str, "image": str, ...}

    Raises:
        ScrapingError: If the API request fails or returns invalid data
    """
    import json

    url = f"{BASE_URL}/api/user.php?id={userid}"
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.footballkitarchive.com/collection/{userid}",
        "User-Agent": HTTP_DEFAULT_HEADERS["User-Agent"],
    }

    logger.info(f"Fetching user info for userid: {userid}")

    try:
        response = http_get(url, headers=headers, use_proxy=True)
        response.raise_for_status()

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response for user info: {str(e)}")
            return None

        if not isinstance(data, dict):
            logger.error("User API response is not a dictionary")
            return None

        if data.get("error") is not None:
            error_msg = data.get("error", "Unknown error")
            logger.warning(f"User API returned error: {error_msg}")
            return None

        user_data = data.get("user")
        if not user_data:
            logger.warning(f"No user data found in response for userid: {userid}")
            return None

        logger.info(f"Successfully fetched user info for userid: {userid}")
        return user_data

    except requests.exceptions.HTTPError as e:
        if e.response and e.response.status_code == 403:
            logger.warning(f"Rate limit or access denied for user info (userid: {userid})")
        else:
            logger.warning(f"HTTP error fetching user info: {str(e)}")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"Request error fetching user info: {str(e)}")
        return None


CUSTOM_FIELDS = ["custom_team", "custom_type", "custom_season", "custom_league", "custom_brand"]


def _scrape_user_collection_fetch_page(
    url: str, page: int, headers: dict, total_skipped: dict
) -> tuple[list, bool, int, int, dict]:
    import json

    response = http_get(url, headers=headers, use_proxy=True)
    response.raise_for_status()
    try:
        data = response.json()
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON response for page {page}: {str(e)}")
        raise ScrapingError(f"Invalid JSON response from API: {str(e)}") from e
    if not isinstance(data, dict):
        raise ScrapingError("API response is not a dictionary")
    if not data.get("success", False):
        error_msg = data.get("error", "Unknown error")
        logger.error(f"API returned success=false for page {page}: {error_msg}")
        raise ScrapingError(f"API returned error: {error_msg}")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ScrapingError("API response 'entries' is not a list")
    filtered_entries, skipped_custom = _scrape_user_collection_filter_entries(
        entries, total_skipped
    )
    more_available = data.get("more_available", False)
    sample = entries[0] if (entries and len(filtered_entries) == 0) else {}
    return filtered_entries, more_available, len(entries), skipped_custom, sample


def _scrape_user_collection_filter_entries(
    entries: list, total_skipped: dict
) -> tuple[list, int]:
    filtered_entries = []
    skipped_custom = 0
    for entry in entries:
        entry_id = entry.get("id", "unknown")
        has_custom = any(entry.get(f) is not None for f in CUSTOM_FIELDS)
        if has_custom:
            skipped_custom += 1
            total_skipped["custom"] += 1
            found = [f for f in CUSTOM_FIELDS if entry.get(f) is not None]
            logger.debug(f"Skipping entry {entry_id} - has custom fields: {found}")
            continue
        cleaned = _clean_collection_entry(entry)
        enriched = _enrich_collection_entry(cleaned)
        filtered_entries.append(enriched)
    return filtered_entries, skipped_custom


def _scrape_user_collection_apply_page(
    all_entries: list,
    filtered_entries: list,
    more_available: bool,
    entries_len: int,
    skipped_custom: int,
    sample: dict,
    page: int,
) -> bool:
    if entries_len > 0:
        logger.info(
            f"Page {page} filtering: {entries_len} total entries, "
            f"{skipped_custom} skipped (custom fields only), "
            f"{len(filtered_entries)} kept (unwanted fields removed from response)"
        )
        if len(filtered_entries) == 0 and sample and any(sample.get(f) for f in CUSTOM_FIELDS):
            logger.warning(
                f"All entries on page {page} were filtered. "
                f"Sample entry {sample.get('id')} has custom_* fields (not null)"
            )
        logger.info(
            f"Page {page}: Retrieved {entries_len} entries, {skipped_custom} skipped (custom fields), "
            f"{len(filtered_entries)} kept (total: {len(all_entries) + len(filtered_entries)})"
        )
    all_entries.extend(filtered_entries)
    if not more_available:
        logger.info(f"No more pages available. Total entries: {len(all_entries)}")
        return True
    return False


def scrape_user_collection_api(userid: int) -> dict:
    """
    Scrape user collection using the collection-feed.php API.

    Args:
        userid: User ID from FootballKitArchive

    Returns:
        dict: Complete collection data with all entries from all pages.
              Structure matches the API response with all entries combined.

    Raises:
        ScrapingError: If the API request fails or returns invalid data
        RateLimitExceededError: If rate limit is exceeded
    """
    all_entries = []
    page = 1
    limit = 20
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.footballkitarchive.com/collection/{userid}",
        "User-Agent": HTTP_DEFAULT_HEADERS["User-Agent"],
    }
    logger.info(f"Starting to scrape user collection for userid: {userid}")
    total_skipped = {"custom": 0, "purchase": 0, "gender": 0}
    user_info = scrape_user_info_api(userid)

    while True:
        url = f"{BASE_URL}/api/collection-feed.php?limit={limit}&page={page}&sort=latest&userid={userid}"
        logger.debug(f"Fetching page {page} from: {url}")
        try:
            filtered_entries, more_available, entries_len, skipped_custom, sample = (
                _scrape_user_collection_fetch_page(url, page, headers, total_skipped)
            )
            if _scrape_user_collection_apply_page(
                all_entries, filtered_entries, more_available, entries_len, skipped_custom, sample, page
            ):
                break
            page += 1
            time.sleep(0.5)
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 403:
                raise RateLimitExceededError("Rate limit exceeded while scraping user collection") from e
            logger.error(f"HTTP error fetching page {page}: {str(e)}")
            raise ScrapingError(f"HTTP error fetching page {page}: {str(e)}") from e
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error fetching page {page}: {str(e)}")
            raise ScrapingError(f"Request error fetching page {page}: {str(e)}") from e

    result = {
        "success": True,
        "entries": all_entries,
        "total_entries": len(all_entries),
        "pages_scraped": page,
    }
    if user_info:
        result["user"] = user_info
    logger.info(
        f"Successfully scraped {len(all_entries)} entries from {page} pages for userid: {userid}. "
        f"Filtered out: {total_skipped['custom']} entries (had custom_* fields)"
    )
    return result


def _clean_collection_entry(entry: dict) -> dict:
    """
    Clean a collection entry by removing unwanted fields.

    Removes:
    - From kit: sponsor_name, footyheadlines_url, credits, template, carry_over,
      userid, added_at, updated_at, release_date, image_preview_url
    - From entry: user block (repeated data), purchase/value fields, gender, printing_type
    - Note: custom_* fields are not removed here because entries with custom_* (not null)
      are already filtered out before this function is called

    Args:
        entry: Raw entry dictionary from API (already filtered for custom_*)

    Returns:
        dict: Cleaned entry with only wanted fields
    """
    cleaned = entry.copy()

    # Remove user block (repeated data)
    cleaned.pop("user", None)

    # Remove purchase/value fields (remove from response, but keep the entry)
    cleaned.pop("purchase_date", None)
    cleaned.pop("purchase_price", None)
    cleaned.pop("currency", None)
    cleaned.pop("estimated_value", None)
    cleaned.pop("value_currency", None)

    # Remove gender and printing_type (remove from response, but keep the entry)
    cleaned.pop("gender", None)
    cleaned.pop("printing_type", None)

    # Clean kit object if present
    if "kit" in cleaned and isinstance(cleaned["kit"], dict):
        kit = cleaned["kit"].copy()
        # Remove unwanted kit fields (regardless of null or not)
        kit.pop("sponsor_name", None)
        kit.pop("footyheadlines_url", None)
        kit.pop("credits", None)
        kit.pop("template", None)
        kit.pop("carry_over", None)
        kit.pop("userid", None)
        kit.pop("added_at", None)
        kit.pop("updated_at", None)
        kit.pop("release_date", None)
        kit.pop("image_preview_url", None)
        cleaned["kit"] = kit

    return cleaned


def _enrich_kit_brand(brand_name: str) -> dict:
    try:
        brand = Brand.objects.filter(name__iexact=brand_name).first()
        if not brand:
            from django.db import connection

            if connection.vendor == "postgresql":
                brand = Brand.objects.filter(name__unaccent__iexact=brand_name).first()
            else:
                brand = Brand.objects.filter(name__icontains=brand_name).first()
        if not brand:
            brand = Brand.objects.filter(name__icontains=brand_name).first()
        if brand:
            return {
                "id": brand.id,
                "name": brand.name,
                "slug": brand.slug,
                "logo": brand.logo,
                "logo_dark": brand.logo_dark,
            }
        logger.debug(f"Brand not found: {brand_name}")
    except Exception as e:
        logger.warning(f"Error enriching brand {brand_name}: {str(e)}")
    return {"name": brand_name}


def _find_club_by_team_name(team_name: str) -> Club | None:
    from django.db import connection

    club = Club.objects.filter(name__iexact=team_name).first()
    if club:
        return club
    if connection.vendor == "postgresql":
        return Club.objects.filter(name__unaccent__iexact=team_name).first()
    return Club.objects.filter(name__icontains=team_name).first()


def _find_club_by_kit_id(kit_id: Any) -> Club | None:
    kit_obj = Kit.objects.filter(kit_id=str(kit_id)).first()
    return kit_obj.team if kit_obj else None


def _find_club_by_kit_url(kit_url: str) -> Club | None:
    if not kit_url or kit_url.count("/") < 2:
        return None
    kit_slug = kit_url.strip("/").split("/")[-2]
    club_slug_candidate = re.sub(
        r"-\d{4}(?:-\d{2}){0,2}", "", kit_slug, flags=re.IGNORECASE
    )
    club_slug_candidate = re.sub(
        r"-(?:home|away|third|gk-\d+|special)-kit$",
        "",
        club_slug_candidate,
        flags=re.IGNORECASE,
    )
    potential = club_slug_candidate.strip("-")
    if not potential:
        return None
    kit_obj = Kit.objects.filter(slug__istartswith=potential).first()
    return kit_obj.team if kit_obj else Club.objects.filter(slug__iexact=potential).first()


def _enrich_kit_club(team_name: str, kit_url: str, kit_id: Any) -> dict:
    try:
        club = (
            _find_club_by_team_name(team_name)
            or (kit_id and _find_club_by_kit_id(kit_id))
            or (kit_url and _find_club_by_kit_url(kit_url))
        )
        if club:
            return {
                "id": club.id,
                "name": club.name,
                "slug": club.slug,
                "logo": club.logo,
                "logo_dark": club.logo_dark,
                "country": str(club.country) if club.country else None,
            }
        logger.debug(f"Club not found: {team_name} (URL: {kit_url})")
    except Exception as e:
        logger.warning(f"Error enriching club {team_name}: {str(e)}")
    return {"name": team_name}


def _enrich_collection_entry(entry: dict) -> dict:
    """
    Enrich a collection entry with Brand and Club data from database.

    Adds:
    - Brand: name, slug, logo, logo_dark (from brand_name)
    - Club: name, slug, logo, logo_dark (from team_name or kit URL)

    Args:
        entry: Cleaned entry dictionary

    Returns:
        dict: Enriched entry with brand and club data
    """
    enriched = entry.copy()
    if "kit" not in enriched or not isinstance(enriched["kit"], dict):
        return enriched
    kit = enriched["kit"].copy()
    brand_name = kit.get("brand_name")
    if brand_name:
        kit["brand"] = _enrich_kit_brand(brand_name)
    team_name = kit.get("team_name")
    if team_name:
        kit["club"] = _enrich_kit_club(
            team_name, kit.get("url", ""), kit.get("id")
        )
    enriched["kit"] = kit
    return enriched
