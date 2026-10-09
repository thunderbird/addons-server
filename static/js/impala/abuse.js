$(function() {
    const $abuse = $('fieldset.abuse');
    if ($abuse.find('legend a').length) {
        const $ol = $abuse.find('ol');
        $ol.hide();
        $abuse.find('legend a, .cancel').click(_pd(function() {
            $ol.slideToggle('fast');
        }));
    }
});
