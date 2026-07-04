"""
Service for kit-related business logic.
"""

import logging
from typing import Any

from django.utils.text import slugify

from core.constants import COLOR_SEPARATOR_SLASH, COMPETITION_SEPARATOR, COMPETITION_SLUG_SUFFIX
from core.models import Color, Competition, Kit

logger = logging.getLogger(__name__)


class KitsService:
    """Service for managing kit-related business logic."""

    @staticmethod
    def process_competitions(kit: Kit, competitions_html: str, slug: str) -> None:
        """
        Process and add competitions to a kit.

        Args:
            kit: The kit to add competitions to
            competitions_html: HTML or plain text containing competition information
            slug: Kit slug for error logging
        """
        # No league on this kit (or the row is absent) — nothing to add. Avoids
        # creating a junk empty-named Competition from an empty string.
        if not competitions_html or not competitions_html.strip():
            return
        try:
            # Check if the input is HTML with links or plain text
            if "<a href=" in competitions_html:
                # Parse HTML to extract competition names from links
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(competitions_html, "html.parser")
                competition_links = soup.find_all("a")

                for link in competition_links:
                    comp_name = link.text.strip()
                    comp_slug = link.get("href", "").replace("/", "")

                    if not comp_name or not comp_slug:
                        continue

                    competition, _ = Competition.objects.get_or_create(slug=comp_slug, defaults={"name": comp_name})
                    kit.competition.add(competition)
            else:
                # Process plain text competitions
                if COMPETITION_SEPARATOR in competitions_html:
                    competitions = competitions_html.split(COMPETITION_SEPARATOR)
                    for comp_name in competitions:
                        comp_name = comp_name.strip()
                        competition, _ = Competition.objects.get_or_create(
                            name=comp_name, slug=slugify(f"{comp_name}{COMPETITION_SLUG_SUFFIX}")
                        )
                        kit.competition.add(competition)
                else:
                    competition, _ = Competition.objects.get_or_create(
                        name=competitions_html.strip(),
                        slug=slugify(f"{competitions_html.strip()}{COMPETITION_SLUG_SUFFIX}"),
                    )
                    kit.competition.add(competition)

            kit.save()
        except Exception as e:
            logger.error(f"Error processing competitions for {kit}: {str(e)}")

    @staticmethod
    def process_colors(kit: Kit, colors_str: str) -> None:
        """
        Process and add colors to a kit.

        Args:
            kit: The kit to add colors to
            colors_str: String containing color information (format: "Primary / Secondary")
        """
        try:
            # Format is typically "Primary / Secondary" or just "Primary"
            colors = [c.strip() for c in colors_str.split(COLOR_SEPARATOR_SLASH.strip())]

            if not colors:
                return

            # Process primary color
            primary_color_name = colors[0]
            primary_color, _ = Color.objects.get_or_create(name=primary_color_name)
            kit.primary_color = primary_color

            # Process secondary colors if any
            if len(colors) > 1:
                for color_name in colors[1:]:
                    if not color_name.strip():
                        continue
                    secondary_color, _ = Color.objects.get_or_create(name=color_name.strip())
                    kit.secondary_color.add(secondary_color)

            kit.save()
        except Exception as e:
            logger.error(f"Error processing colors for {kit}: {str(e)}")

    @staticmethod
    def create_or_update_kit(
        slug: str,
        kit_name: str,
        team: Any,
        season: Any,
        type_k: Any,
        brand: Any,
        rating: float,
        main_img_url: str,
        design: str | None = None,
        kit_id: str | None = None,
        existing_kit_id: int | None = None,
    ) -> tuple[Kit, bool]:
        """
        Create or update a kit in the database.

        Args:
            slug: Kit slug
            kit_name: Kit name
            team: Club object
            season: Season object
            type_k: Type_K object
            brand: Brand object
            rating: Kit rating
            main_img_url: Main image URL
            design: Kit design (optional)
            kit_id: Kit ID from source (optional)

        Returns:
            Tuple of (Kit object, created boolean)
        """
        clean_slug = slug.strip("/").split("/")[0]

        # If we're updating a specific kit, find it by ID first
        existing_kit = None
        if existing_kit_id:
            try:
                existing_kit = Kit.objects.get(id=existing_kit_id)
            except Kit.DoesNotExist:
                pass

        # If not found by ID, check if kit already exists with this team, season, and type
        if not existing_kit:
            existing_kit = Kit.objects.filter(team=team, season=season, type=type_k).first()

        if existing_kit:
            # Kit already exists, update all fields
            needs_update = False

            if existing_kit.name != kit_name:
                logger.debug(f"Updating name: {existing_kit.name} -> {kit_name}")
                existing_kit.name = kit_name
                needs_update = True

            if existing_kit.slug != clean_slug:
                # Only update slug if it doesn't conflict with another kit
                if not Kit.objects.filter(slug=clean_slug).exclude(id=existing_kit.id).exists():
                    logger.debug(f"Updating slug: {existing_kit.slug} -> {clean_slug}")
                    existing_kit.slug = clean_slug
                    needs_update = True
                else:
                    logger.warning(f"Cannot update slug to {clean_slug}: already exists")

            if existing_kit.kit_id != kit_id:
                logger.debug(f"Updating kit_id: {existing_kit.kit_id} -> {kit_id}")
                existing_kit.kit_id = kit_id
                needs_update = True

            if existing_kit.design != design:
                logger.debug(f"Updating design: {existing_kit.design} -> {design}")
                existing_kit.design = design
                needs_update = True

            if existing_kit.rating != rating:
                logger.debug(f"Updating rating: {existing_kit.rating} -> {rating}")
                existing_kit.rating = rating
                needs_update = True

            if existing_kit.main_img_url != main_img_url:
                logger.debug(f"Updating main_img_url: {existing_kit.main_img_url} -> {main_img_url}")
                existing_kit.main_img_url = main_img_url
                needs_update = True

            if existing_kit.brand != brand:
                logger.debug(f"Updating brand: {existing_kit.brand} -> {brand}")
                existing_kit.brand = brand
                needs_update = True

            if needs_update:
                existing_kit.save()
            else:
                logger.debug(f"Kit {existing_kit.slug} already up to date")

            return existing_kit, False
        else:
            # Create new kit
            kit, created = Kit.objects.update_or_create(
                slug=clean_slug,
                defaults={
                    "name": kit_name,
                    "team": team,
                    "season": season,
                    "type": type_k,
                    "brand": brand,
                    "rating": rating,
                    "main_img_url": main_img_url,
                    "design": design,
                    "kit_id": kit_id,
                },
            )
            return kit, created
