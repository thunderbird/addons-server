/**
 * Bubbles up persona event to tell Firefox to load a persona
 **/
function dispatchPersonaEvent(aType, aNode, callback, forceHttps)
{
    var aliases = {'PreviewPersona': 'PreviewBrowserTheme',
                   'ResetPersona': 'ResetBrowserThemePreview',
                   'SelectPersona': 'InstallBrowserTheme'};
    try {
        if (!('browsertheme' in aNode.dataset)) {
            return;
        }

        var browsertheme = $(aNode).attr('data-browsertheme');

        if (forceHttps) {
            browsertheme = browsertheme.replace(/http:\/\//g, 'https://');
        }

        $(aNode).attr('persona', browsertheme);

        var aliasEvent = aliases[aType];
        var events = [aType, aliasEvent];

        for (var i=0; i<events.length; i++) {
          var event = events[i];
          var eventObject = new Event(event, {bubbles: true, cancelable: false});
          aNode.dispatchEvent(eventObject);
        }
        if (callback) {
            callback();
        }
    } catch(e) {
        // Theme events are best-effort; browsers without support just ignore them.
    }
}


$.hasPersonas = function() {
    if (!jQuery.browser.mozilla) return false;

    // Fx 3.6 has lightweight themes (aka personas)
    if (VersionCompare.compareVersions(
        $.browser.version, '1.9.2') > -1) {
        return true;
    }

    var body = document.getElementsByTagName('body')[0];
    try {
        var event = new Event('CheckPersonas', {bubbles: true, cancelable: false});
        body.dispatchEvent(event);
    } catch(e) {
        // No persona support; the attribute check below then returns false.
    }

    return body.getAttribute('personas') == 'true';
};
