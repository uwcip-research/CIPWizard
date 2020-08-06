import json

from pprint import pprint


def extract_images(input_data, types=['photo']):

    # Stolen from https://github.com/morinokami/twitter-image-downloader/blob/master/twt_img/twt_img.py

    if len(types) > 1:
        raise NotImplementedError

    if "media" in input_data["entities"]:

        if "extended_entities" in input_data:
            media_types = [x['type'] for x in input_data["extended_entities"]["media"]]
            extra = [
                x["media_url"] for x in input_data["extended_entities"]["media"] if x['type'] in types
            ]
        else:
            media_types = None
            extra = []

        if all([x in types for x in media_types]):
            urls = [x["media_url"] for x in input_data["entities"]["media"] if x['type'] in types]
            urls = set(urls + extra)
            return urls
    else:
        return None


if __name__ == '__main__':

    pass