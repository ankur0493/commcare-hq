/* global d3 */
var url = hqImport('hqwebapp/js/initial_page_data').reverse;

function AdolescentWomenController($scope, $routeParams, $location, $filter, demographicsService, locationsService,
    userLocationId, storageService, baseControllersService) {
    baseControllersService.BaseController.call(this, $scope, $routeParams, $location, locationsService,
        userLocationId, storageService);
    var vm = this;
    vm.label = "Adolescent Girls (11-14 years)";
    vm.steps = {
        'map': {route: '/adolescent_girls/map', label: 'Map View'},
        'chart': {route: '/adolescent_girls/chart', label: 'Chart View'},
    };
    vm.data = {
        legendTitle: 'Number of Women',
    };
    vm.filters = ['age', 'gender'];
    vm.rightLegend = {
        info: 'Total number of adolescent girls who are enrolled for Anganwadi Services',
    };

    vm.templatePopup = function(loc, row) {
        var valid = $filter('indiaNumbers')(row ? row.valid : 0);
        var all = $filter('indiaNumbers')(row ? row.all : 0);
        var percent = row ? d3.format('.2%')(row.valid / (row.all || 1)) : "N/A";
        return vm.createTemplatePopup(
            loc.properties.name,
            [{
                indicator_name: 'Number of adolescent girls (11 - 14 years) who are enrolled for Anganwadi Services: ',
                indicator_value: valid,
            },
            {
                indicator_name: 'Total number of adolescent girls (11 - 14 years) who are registered: ',
                indicator_value: all,
            },
            {
                indicator_name: 'Percentage of registered adolescent girls (11 - 14 years) who are enrolled for Anganwadi Services: ',
                indicator_value: percent,
            }]
        );
    };

    vm.loadData = function () {
        vm.setStepsMapLabel();
        var usePercentage = false;
        var forceYAxisFromZero = false;
        vm.myPromise = demographicsService.getAdolescentGirlsData(vm.step, vm.filtersData).then(
            vm.loadDataFromResponse(usePercentage, forceYAxisFromZero)
        );
    };

    vm.init();

    vm.chartOptions = {
        chart: {
            type: 'lineChart',
            height: 450,
            width: 1100,
            margin: {
                top: 20,
                right: 60,
                bottom: 60,
                left: 80,
            },
            x: function (d) {
                return d.x;
            },
            y: function (d) {
                return d.y;
            },
            color: d3.scale.category10().range(),
            useInteractiveGuideline: true,
            clipVoronoi: false,
            tooltips: true,
            xAxis: {
                axisLabel: '',
                showMaxMin: true,
                tickFormat: function (d) {
                    return d3.time.format('%b %Y')(new Date(d));
                },
                tickValues: function () {
                    return vm.chartTicks;
                },
                axisLabelDistance: -100,
            },

            yAxis: {
                axisLabel: '',
                tickFormat: function (d) {
                    return d3.format(",")(d);
                },
                axisLabelDistance: 20,
                forceY: [0],
            },
            callback: function (chart) {
                var tooltip = chart.interactiveLayer.tooltip;
                tooltip.contentGenerator(function (d) {

                    var day = _.find(vm.chartData[0].values, function(num) { return num['x'] === d.value; });
                    return vm.tooltipContent(d3.time.format('%b %Y')(new Date(d.value)), day);
                });
                return chart;
            },
        },
        caption: {
            enable: true,
            html: '<i class="fa fa-info-circle"></i> Total number of adolescent girls who are enrolled for Anganwadi Services',
            css: {
                'text-align': 'center',
                'margin': '0 auto',
                'width': '900px',
            },
        },
    };

    vm.tooltipContent = function (monthName, day) {
        return "<p><strong>" + monthName + "</strong></p><br/>"
            + "<div>Number of adolescent girls (11 - 14 years) who are enrolled for Anganwadi Services: <strong>" + $filter('indiaNumbers')(day.y) + "</strong></div>"
            + "<div>Total number of adolescent girls (11 - 14 years) who are registered: <strong>" + $filter('indiaNumbers')(day.all) + "</strong></div>"
            + "<div>Percentage of registered adolescent girls (11 - 14 years) who are enrolled for Anganwadi Services: <strong>" + d3.format('.2%')(day.y / (day.all || 1)) + "</strong></div>";
    };

    vm.showAllLocations = function () {
        return vm.all_locations.length < 10;
    };
}

AdolescentWomenController.$inject = ['$scope', '$routeParams', '$location', '$filter', 'demographicsService', 'locationsService', 'userLocationId', 'storageService', 'baseControllersService'];

window.angular.module('icdsApp').directive('adolescentGirls', function() {
    return {
        restrict: 'E',
        templateUrl: url('icds-ng-template', 'map-chart'),
        bindToController: true,
        scope: {
            data: '=',
        },
        controller: AdolescentWomenController,
        controllerAs: '$ctrl',
    };
});
