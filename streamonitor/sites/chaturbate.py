import re
import requests
from streamonitor.bot import Bot


class Chaturbate(Bot):
    site = 'Chaturbate'
    siteslug = 'CB'

    def __init__(self, username):
        super().__init__(username)
        self.sleep_on_offline = 30
        self.sleep_on_error = 60

    def getVideoUrl(self):
        url = self.lastInfo['url']
        if self.lastInfo.get('cmaf_edge'):
            url = url.replace('playlist.m3u8', 'playlist_sfm4s.m3u8')
            url = re.sub('live-.+amlst', 'live-c-fhls/amlst', url)

        return self.getWantedResolutionPlaylist(url)

    def getStatus(self):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        data = {"room_slug": self.username, "bandwidth": "high"}

        try:
            r = requests.post("https://chaturbate.com/get_edge_hls_url_ajax/", headers=headers, data=data)
            self.lastInfo = r.json()

            if self.lastInfo["room_status"] == "public":
                status = self.Status.PUBLIC
            elif self.lastInfo["room_status"] in ["private", "hidden"]:
                status = self.Status.PRIVATE
            else:
                status = self.Status.OFFLINE
        except:
            status = self.Status.RATELIMIT

        self.ratelimit = status == self.Status.RATELIMIT
        return status


Bot.loaded_sites.add(Chaturbate)
