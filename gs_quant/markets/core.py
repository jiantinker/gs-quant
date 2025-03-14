"""
Copyright 2019 Goldman Sachs.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
from concurrent.futures import Future, ThreadPoolExecutor
import copy
import datetime as dt
import functools
import inflection
import logging
from typing import Optional, Tuple, Union

import pandas as pd

from gs_quant.base import Priceable
from gs_quant.context_base import ContextBaseWithDefault
from gs_quant.datetime.date import business_day_offset
from gs_quant.session import GsSession
from gs_quant.target.common import MarketDataCoordinate as __MarketDataCoordinate
from gs_quant.target.risk import PricingDateAndMarketDataAsOf, RiskMeasure, RiskPosition, RiskRequest

_logger = logging.getLogger(__name__)


class MarketDataCoordinate(__MarketDataCoordinate):

    def __str__(self):
        return "|".join(f or '' for f in (self.mkt_type, self.mkt_asset, self.mkt_class,
                                          '_'.join(self.mkt_point or ()), self.mkt_quoting_style))


class PricingContext(ContextBaseWithDefault):

    """
    A context for controlling pricing and market data behaviour
    """

    def __init__(self,
                 pricing_date: Optional[dt.date] = None,
                 market_data_as_of: Optional[Union[dt.date, dt.datetime]] = None,
                 market_data_location: Optional[str] = None,
                 is_async: bool = False,
                 is_batch: bool = False):
        """
        The methods on this class should not be called directly. Instead, use the methods on the instruments, as per the examples

        :param pricing_date: the date for pricing calculations. Default is today
        :param market_data_as_of: the date/datetime for sourcing market data. Default is 1 business day before pricing_date
        :param market_data_location: the location for sourcing market data ('NYC', 'LDN' or 'HKG'. Default is NYC
        :param is_async: if True, return (a future) immediately. If False, block
        :param is_batch: use for calculations expected to run longer than 3 mins, to avoid timeouts. It can be used with is_aync=True|False

        **Examples**

        To change the market data location of the default context:

        >>> from gs_quant.risk import PricingContext
        >>> import datetime as dt
        >>>
        >>> PricingContext.current = PricingContext(market_data_location='LDN')

        For a blocking, synchronous request:

        >>> from gs_quant.instrument import IRCap
        >>> cap = IRCap('5y', 'GBP')
        >>>
        >>> with PricingContext():
        >>>     price_f = cap.dollar_price()
        >>>
        >>> price = price_f.result()

        For an asynchronous request:

        >>> with PricingContext(is_async=True):
        >>>     price_f = inst.dollar_price()
        >>>
        >>> while not price_f.done():
        >>>     ...
        """
        super().__init__()
        self.__pricing_date = pricing_date or dt.date.today()
        self.__market_data_as_of = market_data_as_of
        self.__market_data_location = market_data_location or (
            self.__class__.current.market_data_location if self.__class__.default_is_set else 'LDN')
        self.__is_async = is_async
        self.__is_batch = is_batch
        self.__risk_measures_by_provider_and_position = {}
        self.__futures = {}

    def _on_exit(self, exc_type, exc_val, exc_tb):
        self._calc()

    def _calc(self):
        from gs_quant.risk import ScenarioContext

        def run_request(request: RiskRequest, session: GsSession):
            calc_result = {}

            try:
                with session:
                    calc_result = provider.calc(request)
            except Exception as e:
                for risk_measure in request.measures:
                    measure_results = {}
                    for result_position in risk_request.positions:
                        measure_results[result_position] = str(e)

                    calc_result[risk_measure] = measure_results
            finally:
                self._handle_results(calc_result)

        from gs_quant.api.risk import RiskApi

        def get_batch_results(request: RiskRequest, session: GsSession,
                              batch_provider: RiskApi, batch_result_id: str):
            with session:
                results = batch_provider.get_results(request, batch_result_id)
            self._handle_results(results)

        batch_results = []
        pool = ThreadPoolExecutor(len(self.__risk_measures_by_provider_and_position)) if self.__is_async else None

        while self.__risk_measures_by_provider_and_position:
            provider, risk_measures_by_position = self.__risk_measures_by_provider_and_position.popitem()
            positions_by_risk_measures = {}
            for position, risk_measures in risk_measures_by_position.items():
                positions_by_risk_measures.setdefault(tuple(risk_measures), []).append(position)

            for risk_measures, positions in positions_by_risk_measures.items():
                risk_request = RiskRequest(
                    tuple(positions),
                    risk_measures,
                    wait_for_results=not self.__is_batch,
                    pricing_location=self.market_data_location,
                    scenario=ScenarioContext.current if ScenarioContext.current.scenario is not None else None,
                    pricing_and_market_data_as_of=self._pricing_market_data_as_of
                )

                if self.__is_batch:
                    batch_results.append((provider, risk_request, provider.calc(risk_request)))
                elif pool:
                    pool.submit(run_request, risk_request, GsSession.current)
                else:
                    run_request(risk_request, GsSession.current)

        for provider, risk_request, result_id in batch_results:
            if pool:
                pool.submit(get_batch_results, risk_request, GsSession.current, provider, result_id)
            else:
                get_batch_results(risk_request, GsSession.current, provider, result_id)

        if pool:
            pool.shutdown(wait=not self.__is_async)

    def _handle_results(self, results: dict):
        for risk_measure, position_results in results.items():
            for position, result in position_results.items():
                positions_for_measure = self.__futures[risk_measure]
                future = positions_for_measure.pop(position)
                future.set_result(result)

                if not positions_for_measure:
                    self.__futures.pop(risk_measure)

    @property
    def _pricing_market_data_as_of(self) -> Tuple[PricingDateAndMarketDataAsOf, ...]:
        return PricingDateAndMarketDataAsOf(self.pricing_date, self.market_data_as_of),

    @property
    def pricing_date(self) -> dt.date:
        """Pricing date"""
        return self.__pricing_date

    @property
    def market_data_as_of(self) -> Union[dt.date, dt.datetime]:
        """Market data as of"""
        if self.__market_data_as_of:
            return self.__market_data_as_of
        elif self.pricing_date == dt.date.today():
            return business_day_offset(self.pricing_date, -1, roll='preceding')
        else:
            return self.pricing_date

    @property
    def market_data_location(self) -> str:
        """Market data location"""
        return self.__market_data_location

    def calc(self, priceable: Priceable,
             risk_measure: RiskMeasure) -> Union[float, dict, pd.DataFrame, pd.Series, Future]:
        """
        Calculate the risk measure for the priceable instrument. Do not use directly, use via instruments

        :param priceable: The priceable (e.g. instrument)
        :param risk_measure: The measure we wish to calculate
        :return: A float, Dataframe, Series or Future (depending on is_async or whether the context is entered)

        **Examples**

        >>> from gs_quant.instrument import IRSwap
        >>> from gs_quant.risk import IRDelta
        >>>
        >>> swap = IRSwap('Pay', '10y', 'USD', fixedRate=0.03)
        >>> delta = swap.calc(IRDelta)
        """
        position = RiskPosition(priceable, priceable.get_quantity())
        future = self.__futures.get(risk_measure, {}).get(position)
        if future is None:
            future = Future()
            self.__risk_measures_by_provider_and_position.setdefault(
                priceable.provider(), {}).setdefault(
                position, set()).add(risk_measure)
            self.__futures.setdefault(risk_measure, {})[position] = future

        if not self._is_entered and not self.__is_async:
            self._calc()
            return future.result()
        else:
            return future

    def resolve_fields(self, priceable: Priceable, in_place: bool) -> Optional[Union[Priceable, Future]]:
        """
        Resolve fields on the priceable which were not supplied. Do not use directly, use via instruments

        :param priceable:  The priceable (e.g. instrument)
        :param in_place:   Resolve in place or return a new Priceable

        **Examples**

        >>> from gs_quant.instrument import IRSwap
        >>>
        >>> swap = IRSwap('Pay', '10y', 'USD')
        >>> rate = swap.fixedRate

        fixedRate is None

        >>> swap.resolve()
        >>> rate = swap.fixedRate

        fixedRate is now the solved value
        """
        # TODO Handle these correctly in the risk service
        invalid_defaults = ('-- N/A --', 'NaN')
        value_mappings = {'Payer': 'Pay', 'Rec': 'Receive', 'Receiver': 'Receive'}

        def apply_field_values(
            field_values: Union[dict, list, tuple, Future],
            priceable_inst: Priceable,
            resolution_info: dict,
            future: Optional[Future] = None
        ):
            if isinstance(field_values, str):
                raise RuntimeError(field_values)

            if isinstance(field_values, Future):
                field_values = field_values.result()

            if isinstance(field_values, (list, tuple)):
                if len(field_values) == 1:
                    field_values = field_values[0]
                else:
                    future.set_result(
                        {dt.date.fromtimestamp(fv['date'] / 1e9): apply_field_values(fv, priceable_inst)
                         for fv in field_values})
                    return

            field_values = {field: value_mappings.get(value, value) for field, value in field_values.items()
                            if inflection.underscore(field) in priceable_inst.properties() and value not in invalid_defaults}

            if in_place and not future:
                priceable_inst.unresolved = copy.copy(priceable_inst)
                for field, value in field_values.items():
                    setattr(priceable_inst, field, value)

                priceable_inst._resolution_info = resolution_info
            else:
                new_inst = priceable_inst._from_dict(field_values)
                new_inst.unresolved = priceable_inst
                new_inst._resolution_info = resolution_info

                if future:
                    future.set_result(new_inst)
                else:
                    return new_inst

        resolution_info = {
            'pricing_date': self.pricing_date,
            'market_data_as_of': self.market_data_as_of,
            'market_data_location': self.market_data_location}

        if priceable._resolution_info:
            if in_place:
                if resolution_info != priceable._resolution_info:
                    _logger.warning('Calling resolve() on an instrument which was already resolved under a difference PricingContext')

                return
            elif resolution_info == priceable._resolution_info:
                return copy.copy(priceable)

        res = self.calc(priceable, RiskMeasure(measure_type='Resolved Instrument Values'))
        if isinstance(res, Future):
            ret = Future() if not in_place else None
            res.add_done_callback(functools.partial(apply_field_values,
                                                    priceable_inst=priceable,
                                                    resolution_info=resolution_info,
                                                    future=ret))
            return ret
        else:
            return apply_field_values(res, priceable, resolution_info)
