from ontology.config import EmbeddingIdentity

from .config import Profile


def identity_for(profile: Profile) -> EmbeddingIdentity:
    return EmbeddingIdentity(
        model=profile.identity_model or profile.embedding_model,
        revision=profile.embedding_revision,
        dimension=profile.embedding_dimensions,
        query_template=profile.query_template,
    )
