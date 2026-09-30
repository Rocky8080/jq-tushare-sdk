"""Strict as-of security names, ST flags and provider-published price limits."""
from collections import OrderedDict

import pandas as pd


class HistoricalSecurityStateError(ValueError):
    pass


class HistoricalSecurityState:
    def __init__(self, backend):
        self.backend = backend
        self.names = None
        self.code_aliases = None
        self.days = OrderedDict()

    def day(self, day):
        if day in self.days:
            self.days.move_to_end(day)
            return self.days[day]
        coverage = self.backend.fetch('security_state_coverage', trade_date=day)
        if not {'api_name', 'row_count'}.issubset(coverage.columns):
            raise HistoricalSecurityStateError(
                f'security_state_coverage missing for {day}; update stock_st/stk_limit complete daily history')
        frames = {}
        for api in ('stock_st', 'stk_limit'):
            frame = self.backend.fetch(api, trade_date=day)
            marker = coverage[coverage.api_name == api]
            if len(marker) != 1 or len(frame) != int(marker.iloc[0].row_count) or frame.empty:
                raise HistoricalSecurityStateError(
                    f'{api} snapshot missing/incomplete for {day}; update complete daily history before backtesting')
            frames[api] = {str(r['ts_code']): r for r in frame.to_dict('records')}
        if self.code_aliases is None:
            mapping = self.backend.fetch('bse_mapping')
            self.code_aliases = (
                dict(zip(mapping.o_code, mapping.n_code))
                if {'o_code', 'n_code'}.issubset(mapping.columns) else {}
            )
        # Provider daily prices/namechange use new BSE codes retroactively,
        # whereas stock_st retains the original codes before the migration.
        for old, new in self.code_aliases.items():
            if old in frames['stock_st']:
                frames['stock_st'][new] = frames['stock_st'][old]
        frames['names'] = self.names_on(day)
        self.days[day] = frames
        while len(self.days) > 4:
            self.days.popitem(last=False)
        return frames

    def names_on(self, day):
        if self.names is None:
            self.names = self.backend.fetch('namechange')
        if self.names.empty:
            return {}
        if not {'ts_code', 'name', 'start_date', 'end_date'}.issubset(self.names.columns):
            raise HistoricalSecurityStateError('namechange history has missing effective interval columns')
        ends = self.names.end_date.fillna('')
        names = self.names[(self.names.start_date <= day) & ((ends == '') | (ends >= day))]
        # Providers may leave an old interval open after a rename. Prefer the
        # latest effective start, but do not guess between equally dated names.
        latest = names.groupby('ts_code').start_date.transform('max')
        names = names[names.start_date == latest]
        conflicts = names.groupby('ts_code').name.nunique()
        if (conflicts > 1).any():
            codes = ','.join(conflicts[conflicts > 1].index[:5])
            raise HistoricalSecurityStateError(f'Conflicting name intervals at {day}: {codes}')
        return dict(zip(names.ts_code, names.name))

    def resolve(self, code, day, *, paused=False):
        frames = self.day(day)
        st_row = frames['stock_st'].get(code)
        name = frames['names'].get(code)
        name_source = 'namechange' if name else 'stock_st' if st_row else 'security_code'
        # Some renamed/re-coded securities have no namechange response under the
        # original code. Use a truthful code label, NOT their current name. ST is
        # still determined by the independently complete daily ST snapshot.
        name = name or (st_row['name'] if st_row else code)
        named_st = 'ST' in str(name).upper().replace('＊', '*')
        is_st = st_row is not None
        if not paused and named_st != is_st:
            raise HistoricalSecurityStateError(f'ST/name history conflict for {code} on {day}: {name}')
        # Suspended securities can be absent from the ST snapshot; an effective
        # ST name remains affirmative historical evidence, not a current-name guess.
        is_st = is_st or named_st
        limits = frames['stk_limit'].get(code)
        if (limits is None or any(pd.isna(limits.get(k)) for k in ('up_limit', 'down_limit'))
                or float(limits['up_limit']) <= 0 or float(limits['down_limit']) < 0):
            if paused:
                return str(name), is_st, 0.0, 0.0, name_source
            # Missing limits are NOT automatically interpreted as unlimited IPO days.
            raise HistoricalSecurityStateError(f'Missing valid official limits for {code} on {day}')
        high, low = float(limits['up_limit']), float(limits['down_limit'])
        # Preserve explicit provider values (e.g. IPO 99999.99 / 0.0), never
        # manufacture an unlimited day merely because a row is absent.
        if high < low:
            raise HistoricalSecurityStateError(f'Invalid official limits for {code} on {day}')
        return str(name), is_st, high, low, name_source
