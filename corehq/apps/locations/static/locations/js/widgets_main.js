hqDefine("locations/js/widgets_main", function() {
    $(function() {
        $(".locations-widget-autocomplete").each(function() {
            var $select = $(this),
                options = $select.data();
            $select.select2({
                placeholder: gettext("Select a Location"),
                allowClear: true,
                multiple: options.multiselect,
                ajax: {
                    url: options.queryUrl,
                    dataType: 'json',
                    quietMillis: 500,
                    data: function (term, page) {
                        return {
                            name: term,
                            page: page
                        };
                    },
                    results: function (data, page) {
                        // 10 results per query
                        var more = (page * 10) < data.total_count;
                        return {results: data.results, more: more};
                    }
                },
                initSelection: function(element, callback) {
                    callback(options.initialData);
                    $(element).trigger('select-ready');
                },
                formatResult: function(e) { return e.name; },
                formatSelection: function(e) { return e.name; }
            });
        });
    });
});
