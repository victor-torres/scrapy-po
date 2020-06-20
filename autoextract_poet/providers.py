from typing import Type

from scrapy.http import Request
from scrapy.statscollectors import StatsCollector
from scrapy_poet.page_input_providers import (
    PageObjectInputProvider,
    provides,
)

from autoextract_poet.inputs import (
    ProductResponseData,
    ProductListResponseData,
)
from autoextract_poet.utils import request


class AutoExtractProvider(PageObjectInputProvider):

    page_type: str
    page_input_class: Type

    def __init__(self, request: Request, stats: StatsCollector):
        self.request = request
        self.stats = stats

    async def __call__(self):
        self.stats.inc_value(f"autoextract/{self.page_type}/total")

        try:
            # FIXME: how to configure if you want full HTML or not?
            data = await request(self.request.url, self.page_type)
        except Exception:
            self.stats.inc_value(f"autoextract/{self.page_type}/error")
            raise

        self.stats.inc_value(f"autoextract/{self.page_type}/success")
        return self.page_input_class(data=data)


@provides(ProductResponseData)
class AutoExtractProductResponseDataProvider(AutoExtractProvider):

    page_type = "product"
    page_input_class = ProductResponseData


@provides(ProductListResponseData)
class AutoExtractProductListResponseDataProvider(AutoExtractProvider):

    page_type = "productList"
    page_input_class = ProductListResponseData