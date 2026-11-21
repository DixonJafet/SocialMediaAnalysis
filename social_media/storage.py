"""Store publication data and links in their MongoDB databases."""

from bson import ObjectId


COLLECTION = "SocialMediaDB"


def collections(client):
    return client["SocialMediaDB"][COLLECTION], client["SocialMediaDBInfo"][COLLECTION]


def ensure_link_index(links):
    indexes = links.index_information()
    if not any(index.get("unique") and index.get("key") == [("instagram_url", 1)]
               for index in indexes.values()):
        raise ValueError("The existing unique instagram_url index is required")


def split_pair(pair):
    if not isinstance(pair.get("instagram"), dict) or not isinstance(pair.get("facebook"), dict):
        raise ValueError("Both networks must be present")
    instagram = pair["instagram"].copy()
    facebook = pair["facebook"].copy()
    ig_url = instagram.pop("url", None)
    fb_url = facebook.pop("url", None)
    if not isinstance(ig_url, str) or not ig_url or not isinstance(fb_url, str) or not fb_url:
        raise ValueError("Both publication URLs are required")
    fields = {key: value for key, value in pair.items() if key != "_id"}
    fields["instagram"], fields["facebook"] = instagram, facebook
    return fields, ig_url, fb_url


def save_pair(client, pair):
    source, links = collections(client)
    fields, ig_url, fb_url = split_pair(pair)
    instagram, facebook = fields.pop("instagram"), fields.pop("facebook")
    ig_caption, fb_caption = instagram.pop("caption"), facebook.pop("caption")
    published_at = instagram.pop("published_at")
    fb_date = facebook.pop("published_date_display")
    if not all(isinstance(value, str) and value.strip() for value in (ig_caption, fb_caption)):
        raise ValueError("Both publication captions are required")

    def write(session):
        if source.find_one({"_id": ig_url}, {"_id": 1}, session=session):
            raise ValueError("Legacy publication found; migrate links before writing")
        mapping = links.find_one({"instagram_url": ig_url}, session=session)
        metadata = {
            **fields,
            "instagram_url": ig_url,
            "facebook_url": fb_url,
            "original_publication_fields.instagram.caption": ig_caption,
            "original_publication_fields.instagram.published_at": published_at,
            "original_publication_fields.facebook.caption": fb_caption,
            "original_publication_fields.facebook.published_date_display": fb_date,
        }
        if mapping is None:
            identifier = ObjectId()
            source.insert_one({
                "_id": identifier, "caption": fb_caption, "published_at": published_at,
                "instagram": instagram, "facebook": facebook,
            }, session=session)
            links.update_one({"_id": identifier}, {
                "$set": metadata, "$setOnInsert": {"id_original": ig_url},
            }, upsert=True, session=session)
        else:
            identifier = mapping["_id"]
            existing = source.find_one({"_id": identifier}, session=session)
            if existing is None:
                raise ValueError("Link mapping has no corresponding publication")
            if not isinstance(existing.get("caption"), str) or not existing["caption"].strip():
                raise ValueError("Existing publication has no shared caption")
            updates = {}
            unset = {}
            for network, scraped in (("instagram", instagram), ("facebook", facebook)):
                previous = existing.get(network)
                if not isinstance(previous, dict):
                    raise ValueError(f"Existing publication is missing {network}")
                updates.update({f"{network}.{key}": value for key, value in scraped.items()})
                if previous.get("comments") != scraped.get("comments"):
                    unset[f"{network}.comment_labels"] = ""
            operation = {"$set": updates}
            if unset:
                operation["$unset"] = unset
            source.update_one({"_id": identifier}, operation, session=session)
            links.update_one({"_id": identifier}, {"$set": metadata}, session=session)

    with client.start_session() as session:
        session.with_transaction(write)
