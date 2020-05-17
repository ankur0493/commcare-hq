from corehq.apps.cleanup.management.commands.populate_sql_model_from_couch_model import PopulateSQLCommand
from corehq.apps.commtrack.models import (
    AlertConfig,
    ActionConfig,
    ConsumptionConfig,
    StockLevelsConfig,
    StockRestoreConfig,
)


class Command(PopulateSQLCommand):
    @classmethod
    def couch_doc_type(self):
        return 'CommtrackConfig'

    @classmethod
    def sql_class(self):
        from corehq.apps.commtrack.models import CommtrackConfig
        return CommtrackConfig

    @classmethod
    def commit_adding_migration(cls):
        return None

    @classmethod
    def attrs_to_sync(cls):
        return [
            "domain",
            "use_auto_emergency_levels",
            "sync_consumption_fixtures",
            "use_auto_consumption",
            "individual_consumption_defaults",
        ]

    @classmethod
    def one_to_one_submodels(cls):
        return [
            {
                "sql_class": AlertConfig,
                "couch_attr": "alert_config",
                "fields": ['stock_out_facilities', 'stock_out_commodities', 'stock_out_rates', 'non_report'],
            },
            {
                "sql_class": ConsumptionConfig,
                "couch_attr": "consumption_config",
                "fields": [
                    'min_transactions', 'min_window', 'optimal_window',
                    'use_supply_point_type_default_consumption', 'exclude_invalid_periods',
                ]
            },
            {
                "sql_class": StockLevelsConfig,
                "couch_attr": "stock_levels_config",
                "fields": ['emergency_level', 'understock_threshold', 'overstock_threshold'],
            },
            {
                "sql_class": StockRestoreConfig,
                "couch_attr": "ota_restore_config",
                "fields": [
                    'section_to_consumption_types', 'force_consumption_case_types', 'use_dynamic_product_list',
                ],
                "wrap": self._wrap_stock_restore_config,
            },
        ]

    @classmethod
    def _wrap_stock_restore_config(cls, doc):
        if 'force_to_consumption_case_types' in doc:
            realval = doc['force_to_consumption_case_types']
            oldval = doc.get('force_consumption_case_types')
            if realval and not oldval:
                doc['force_consumption_case_types'] = realval
        return doc

    def update_or_create_sql_object(self, doc):
        model, created = self.sql_class().objects.update_or_create(
            couch_id=doc['_id'],
            defaults={
                attr: doc.get("attr") for attr in self.attrs_to_sync()
            },
        )

        for spec in self.one_to_one_submodels():
            couch_submodel = getattr(doc, spec.couch_attr)
            if spec.wrap:
                couch_submodel = spec.wrap(self, couch_submodel)
            setattr(self, spec.sql_class.__name__.lower(), spec.sql_class(**{
                field: getattr(couch_submodel, field)
                for field in spec.fields
            }))

        sql_actions = []
        for a in doc['actions']:
            subaction = doc.get('subaction')
            if doc.get('name', '') == 'lost':
                subaction == 'loss'
            sql_actions.append(ActionConfig(
                action=doc.get('action_type', doc.get('action')),
                subaction=subaction,
                _keyword=doc.get('_keyword'),
                caption=doc.get('caption'),
            ))
        model.set_actions(sql_actions)

        model.save(sync_to_couch=False)
        return (model, created)
