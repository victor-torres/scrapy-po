import inspect
from typing import Any, Callable, Dict, List, Optional, Set, Type

import andi
from scrapy import Spider
from scrapy.crawler import Crawler
from scrapy.http import Request, Response
from scrapy.settings import Settings
from scrapy.statscollectors import StatsCollector
from scrapy.utils.defer import maybeDeferred_coro
from twisted.internet.defer import inlineCallbacks, returnValue

from web_poet.pages import ItemPage, is_injectable
from scrapy_poet.page_input_providers import PageObjectInputProvider

_CALLBACK_FOR_MARKER = '__scrapy_poet_callback'

_SCRAPY_PROVIDED_CLASSES = {
    Spider,
    Request,
    Response,
    Crawler,
    Settings,
    StatsCollector,
}


def get_provided_classes_from_providers(providers: List[Type[PageObjectInputProvider]]) -> Set[Type]:
    provided_classes = (p.provided_classes for p in providers)
    return set.union(*provided_classes)


def get_callback(request, spider):
    """Get request.callback of a scrapy.Request, as a callable."""
    if request.callback is None:
        return getattr(spider, 'parse')
    return request.callback


class DummyResponse(Response):
    """This class is returned by the ``InjectionMiddleware`` when it detects
    that the download could be skipped. It inherits from Scrapy ``Response``
    and signals and stores the URL and references the original ``Request``.

    If you want to skip downloads, you can type annotate your parse method
    with this class.

    .. code-block:: python

        def parse(self, response: DummyResponse):
            pass

    If there's no Page Input that depends on a Scrapy ``Response``, the
    ``InjectionMiddleware`` is going to skip download and provide a
    ``DummyResponse`` to your parser instead.
    """

    def __init__(self, url: str, request=Optional[Request]):
        super().__init__(url=url, request=request)


def is_callback_using_response(callback: Callable):
    """Check whether the request's callback method is going to use response."""
    if getattr(callback, _CALLBACK_FOR_MARKER, False) is True:
        # The callback_for function was used to create this callback.
        return False

    signature = inspect.signature(callback)
    first_parameter_key = next(iter(signature.parameters))
    first_parameter = signature.parameters[first_parameter_key]
    if str(first_parameter).startswith('*'):
        # Parse method is probably using *args and **kwargs annotation.
        # Let's assume response is going to be used.
        return True

    if first_parameter.annotation is first_parameter.empty:
        # There's no type annotation, so we're probably using response here.
        return True

    if issubclass(first_parameter.annotation, DummyResponse):
        # Type annotation is DummyResponse, so we're probably NOT using it.
        return False

    # Type annotation is not DummyResponse, so we're probably using it.
    return True


def is_provider_using_response(provider):
    """Check whether injectable provider makes use of a valid Response."""
    plan = andi.plan(
        provider,
        is_injectable=is_injectable,
        externally_provided=_SCRAPY_PROVIDED_CLASSES,
    )
    for possible_type, _ in plan:
        if issubclass(possible_type, Response):
            return True

    return False


def discover_callback_providers(callback: Callable, providers: List[Type[PageObjectInputProvider]]):
    plan = andi.plan(
        callback,
        is_injectable=is_injectable,
        externally_provided=get_provided_classes_from_providers(providers),
    )
    result = set()
    for obj, _ in plan:
        for provider in providers:
            if obj in provider.provided_classes:
                result.add(provider)

    return result


def is_response_going_to_be_used(request, spider):
    """Check whether the request's response is going to be used."""
    callback = get_callback(request, spider)
    if is_callback_using_response(callback):
        return True

    providers = spider.settings["SCRAPY_POET_PROVIDERS"]
    for provider in discover_callback_providers(callback, providers):
        if is_provider_using_response(provider):
            return True

    return False


def build_plan(callback: Callable, providers: List[Type[PageObjectInputProvider]]) -> andi.Plan:
    """Build a plan for the injection in the callback."""
    return andi.plan(
        callback,
        is_injectable=is_injectable,
        externally_provided=get_provided_classes_from_providers(providers),
    )


def build_provider(provider: Type[PageObjectInputProvider],
                   external_dependencies: Dict[Callable, Any]) -> PageObjectInputProvider:
    kwargs = andi.plan(
        provider,
        is_injectable=is_injectable,
        externally_provided=external_dependencies.keys(),
        full_final_kwargs=True,
    ).final_kwargs(external_dependencies)
    return provider(**kwargs)  # type: ignore


@inlineCallbacks
def build_instances(plan: andi.Plan, providers: List[Type[PageObjectInputProvider]],
                    external_dependencies: Dict[Callable, Any]):
    """Build the instances dict from a plan including external dependencies."""
    instances = {}

    # Build dependencies handled by registered providers
    dependencies_set = set(cls for cls, kwargs_spec in plan.dependencies)
    for provider in providers:
        provided_classes = dependencies_set & provider.provided_classes
        provided_classes -= instances.keys()
        if not provided_classes:
            continue

        provider_instance = build_provider(provider, external_dependencies)
        results = yield maybeDeferred_coro(provider_instance, provided_classes)
        instances.update(results)

    # Build remaining dependencies
    for cls, kwargs_spec in plan.dependencies:
        if cls not in instances.keys():
            instances[cls] = cls(**kwargs_spec.kwargs(instances))

    raise returnValue(instances)


def callback_for(page_cls: Type[ItemPage]) -> Callable:
    """Create a callback for an :class:`web_poet.pages.ItemPage` subclass.

    The generated callback returns the output of the
    ``ItemPage.to_item()`` method, i.e. extracts a single item
    from a web page, using a Page Object.

    This helper allows to reduce the boilerplate when working
    with Page Objects. For example, instead of this:

    .. code-block:: python

        class BooksSpider(scrapy.Spider):
            name = 'books'
            start_urls = ['http://books.toscrape.com/']

            def parse(self, response):
                links = response.css('.image_container a')
                yield from response.follow_all(links, self.parse_book)

            def parse_book(self, response: DummyResponse, page: BookPage):
                return page.to_item()

    It allows to write this:

    .. code-block:: python

        class BooksSpider(scrapy.Spider):
            name = 'books'
            start_urls = ['http://books.toscrape.com/']

            def parse(self, response):
                links = response.css('.image_container a')
                yield from response.follow_all(links, self.parse_book)

            parse_book = callback_for(BookPage)

    The generated callback could be used as a spider instance method or passed
    as an inline/anonymous argument. Make sure to define it as a spider
    attribute (as shown in the example above) if you're planning to use
    disk queues, because in this case Scrapy is able to serialize
    your request object.
    """
    if not issubclass(page_cls, ItemPage):
        raise TypeError(
            f'{page_cls.__name__} should be a subclass of ItemPage.')

    if getattr(page_cls.to_item, '__isabstractmethod__', False):
        raise NotImplementedError(
            f'{page_cls.__name__} should implement to_item method.')

    # When the callback is used as an instance method of the spider, it expects
    # to receive 'self' as its first argument. When used as a simple inline
    # function, it expects to receive a response as its first argument.
    #
    # To avoid a TypeError, we need to receive a list of unnamed arguments and
    # a dict of named arguments after our injectable.
    def parse(*args, page: page_cls, **kwargs):  # type: ignore
        yield page.to_item()  # type: ignore

    setattr(parse, _CALLBACK_FOR_MARKER, True)
    return parse
