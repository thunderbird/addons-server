// Hijack "Admin / Editor Log in" context menuitem.
$('#admin-login').click(function() {
    window.location = $(this).attr('data-url');
});


// Recaptcha
var RecaptchaOptions = { theme : 'custom' };

$('#recaptcha_different').click(function(e) {
    e.preventDefault();
    Recaptcha.reload();
});

$('#recaptcha_audio').click(function(e) {
    e.preventDefault();
    // HTML lowercases attribute names, so data-nextType is dataset.nexttype.
    var toggleType = this.dataset.nexttype || 'audio';
    Recaptcha.switch_type(toggleType);
    this.dataset.nexttype = toggleType === 'audio' ? 'image' : 'audio';
});

$('#recaptcha_help').click(function(e) {
    e.preventDefault();
    Recaptcha.showhelp();
});
