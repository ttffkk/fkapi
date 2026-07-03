from ninja import Schema


class ClubSerializer(Schema):
    id: int
    name: str
    slug: str
    # logo is nullable in the model (a club may be scraped before its logo is
    # fetched); match the other serializers so /clubs/search doesn't 500 on
    # pydantic validation. See homelab #170.
    logo: str | None = None

    def __init__(self, instance=None):
        super().__init__(instance)


class KitSerializer(Schema):
    id: int
    name: str
    main_img_url: str


class SeasonSerializer(Schema):
    id: int
    year: str


class ClubJsonSchema(Schema):
    id: int | None = None
    id_fka: int | None = None
    name: str
    slug: str
    logo: str | None = None
    logo_dark: str | None = None
    country: str | None = None


class SeasonJsonSchema(Schema):
    id: int | None = None
    year: str
    first_year: str
    second_year: str | None = None


class CompetitionJsonSchema(Schema):
    id: int | None = None
    name: str
    slug: str
    logo: str | None = None
    logo_dark: str | None = None
    country: str | None = None


class TypeJsonSchema(Schema):
    id: int
    name: str
    category: str
    category_order: int
    order_priority: int
    is_goalkeeper: bool


class BrandJsonSchema(Schema):
    id: int | None = None
    name: str
    slug: str
    logo: str | None = None
    logo_dark: str | None = None


class ColorJsonSchema(Schema):
    name: str
    color: str


class KitJsonSchema(Schema):
    name: str
    slug: str
    team: ClubJsonSchema
    season: SeasonJsonSchema
    competition: list[CompetitionJsonSchema]
    type: TypeJsonSchema
    brand: BrandJsonSchema
    design: str | None = None
    primary_color: ColorJsonSchema | None = None
    secondary_color: list[ColorJsonSchema] | None = None
    main_img_url: str


class ClubBulkSchema(Schema):
    """Reduced club schema for bulk responses."""

    name: str
    logo: str | None = None
    logo_dark: str | None = None
    country: str | None = None


class SeasonBulkSchema(Schema):
    """Reduced season schema for bulk responses."""

    year: str


class BrandBulkSchema(Schema):
    """Reduced brand schema for bulk responses."""

    name: str
    logo: str | None = None
    logo_dark: str | None = None


class KitBulkSchema(Schema):
    """Reduced kit schema for bulk responses."""

    name: str
    team: ClubBulkSchema
    season: SeasonBulkSchema
    brand: BrandBulkSchema
    main_img_url: str
