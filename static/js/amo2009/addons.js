
/* Kept from amo2009 (jbalogh); keep-or-drop is tracked in thunderbird/addons-server#464. */
/**
 * bandwagon: fire a custom refresh event for bandwagon extension
 * @return void
 */
function bandwagonRefreshEvent() {
    if (document.createEvent) {
        var bandwagonSubscriptionsRefreshEvent = new Event("bandwagonRefresh", {bubbles: true, cancelable: false});
        document.dispatchEvent(bandwagonSubscriptionsRefreshEvent);
    }
}

/* Kept from amo2009 (jbalogh); keep-or-drop is tracked in thunderbird/addons-server#464. */
/* Remove "Go" buttons from <form class="go" */
$(document).ready(function(){
    $('form.go').change(function() { this.submit(); })
        .find('button').hide();
});


// Kept from amo2009 (jbalogh); keep-or-drop is tracked in thunderbird/addons-server#464.
var AMO = {};
