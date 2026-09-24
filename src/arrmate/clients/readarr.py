"""Readarr API client implementation."""

from pydantic import Field, TypeAdapter

from .base_arr import ArrRecord, BaseArrClient, Image, MetadataProfile


class _AuthorFields(ArrRecord):
    author_name: str = Field(alias="authorName")
    foreign_author_id: str = Field(alias="foreignAuthorId")
    overview: str | None = None
    images: list[Image] = Field(default_factory=list)
    monitored: bool = False
    tags: list[int] = Field(default_factory=list)

    @property
    def title(self) -> str:
        return self.author_name


class AuthorLookup(_AuthorFields):
    id: int | None = None


class Author(_AuthorFields):
    id: int


class Book(ArrRecord):
    title: str
    foreign_book_id: str = Field(alias="foreignBookId")
    id: int | None = None
    author_id: int | None = Field(default=None, alias="authorId")
    release_date: str | None = Field(default=None, alias="releaseDate")


class SearchResult(ArrRecord):
    """One hit of the combined search: an author or a book, never both."""

    foreign_id: str = Field(alias="foreignId")
    author: AuthorLookup | None = None
    book: Book | None = None


class ReadarrClient(BaseArrClient[Author, SearchResult]):
    """Client for Readarr v1 API (Books/Audiobooks).

    WARNING: Readarr project is deprecated. This client is provided
    for compatibility with existing instances only.
    """

    entity = "author"
    api_prefix = "api/v1"
    search_command = "AuthorSearch"

    async def search(self, query: str) -> list[SearchResult]:
        """Search for books/audiobooks by title or author."""
        raw = await self._get("api/v1/search", params={"term": query})
        return TypeAdapter(list[SearchResult]).validate_python(raw)

    async def add_author(
        self,
        foreign_author_id: str,
        author_name: str,
        quality_profile_id: int,
        metadata_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_missing: bool = True,
    ) -> Author:
        raw = await self._post(
            "api/v1/author",
            data={
                "foreignAuthorId": foreign_author_id,
                "authorName": author_name,
                "qualityProfileId": quality_profile_id,
                "metadataProfileId": metadata_profile_id,
                "rootFolderPath": root_folder_path,
                "monitored": monitored,
                "addOptions": {"searchForMissingBooks": search_for_missing},
            },
        )
        return Author.model_validate(raw)

    async def get_metadata_profiles(self) -> list[MetadataProfile]:
        raw = await self._get("api/v1/metadataprofile")
        return TypeAdapter(list[MetadataProfile]).validate_python(raw)
