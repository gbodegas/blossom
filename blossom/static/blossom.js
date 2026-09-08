/*
  One enhancement, for the forms that make a plan: say that a plan is being
  made, and let one press be one request from that form. Everything on both
  pages works with this file absent; a form without JavaScript submits as it
  always did. Only forms marked data-pending take part, and their submit
  buttons carry no name, so disabling one changes nothing about what is sent.
  Forms whose buttons carry a decision or a step are left alone.
*/
(function () {
  "use strict";

  var forms = document.querySelectorAll("form[data-pending]");

  function reset(form) {
    delete form.dataset.busy;
    form.removeAttribute("aria-busy");
    var button = form.querySelector("button[type=submit]");
    if (button) {
      button.disabled = false;
      if (button.dataset.label) {
        button.textContent = button.dataset.label;
      }
    }
    var note = form.querySelector(".pending");
    if (note) {
      note.remove();
    }
  }

  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.busy) {
        event.preventDefault();
        return;
      }
      form.dataset.busy = "1";
      form.setAttribute("aria-busy", "true");
      var button = form.querySelector("button[type=submit]");
      if (button) {
        button.dataset.label = button.textContent;
        button.disabled = true;
        button.textContent = form.dataset.pending;
      }
      var note = document.createElement("p");
      note.className = "pending";
      note.setAttribute("role", "status");
      note.textContent = form.dataset.pending;
      form.appendChild(note);
    });
  });

  /* A page restored from the browser's cache comes back as it was left, busy
     included; the controls are handed back. */
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      Array.prototype.forEach.call(forms, reset);
    }
  });
})();
